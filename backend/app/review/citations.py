"""固定文献核查：有据抽取、只读数据库核对、引用对应意见，不作交付通过判断。"""

import hashlib
import re
import time
import unicodedata
from dataclasses import asdict
from uuid import UUID, uuid4

from app.review.citation_databases import CitationQuery, CitationVerification
from app.review.extract_types import ExtractedDocument, TextLocation
from app.review.reviewers.citation_types import (
    CitationAlignmentOutput,
    CitationExtractionOutput,
)
from app.review.reviewers.executor import ReviewerExecutor, _safe_json
from app.review.reviewers.instructions import load_citation_instruction
from app.review.reviewers.schemas import HistoricalOpinion
from app.services.llm_usage import QuotaExceededError

DATABASE_RULE_VERSION = "citation-database/1"
MAX_DATABASE_QUERIES = 100
DATABASE_BUDGET_SECONDS = 120
_DATABASE_RULE = (
    "在两个数据库充分查询均成功且无匹配时，仅对非中文文献报告本次数据库查无此条；"
    "中文未检出、弱匹配、元数据冲突及接口故障均未能核验，不证明全球不存在。"
)
_COORDINATES = tuple(TextLocation.__dataclass_fields__)
_DOI = re.compile(r"10\.\d{4,9}/[A-Za-z0-9][A-Za-z0-9._;()/:+%-]{0,200}", re.I)


def _normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _cache_text(value):
    # 验证摘录可忽略标点，缓存却不能合并re-sign与resign等不同检索键。
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _segment_text(location, document):
    coordinates = location.model_dump(exclude={"description"})
    if coordinates["part"] is None:
        raise ValueError
    texts = tuple(
        segment.text
        for segment in document.segments
        if all(coordinates[key] == getattr(segment.location, key) for key in _COORDINATES)
    )
    if not texts:
        raise ValueError
    return texts


def _reference_text(reference):
    return "\n".join(piece.excerpt for piece in reference.evidence)


def _validate_extraction(output, document):
    if not isinstance(output, CitationExtractionOutput) or output.findings:
        raise ValueError
    if len({reference.reference_id for reference in output.references}) != len(output.references):
        raise ValueError
    if output.bibliography_status == "complete" and not output.references:
        raise ValueError
    if output.bibliography_status in {"absent", "unreadable"} and output.references:
        raise ValueError
    if not document.segments and output.bibliography_status != "unreadable":
        raise ValueError
    used = set()
    for reference in output.references:
        for piece in reference.evidence:
            if not any(piece.excerpt in text for text in _segment_text(piece.location, document)):
                raise ValueError
            identity = (*[getattr(piece.location, key) for key in _COORDINATES], piece.excerpt)
            if identity in used:
                raise ValueError
            used.add(identity)
        raw = _reference_text(reference)
        if len(raw) > 4000:
            raise ValueError
        normalized = _normalized(raw)
        for field in (reference.title, *reference.authors):
            if field is not None and (
                not _normalized(field) or _normalized(field) not in normalized
            ):
                raise ValueError
        if reference.year is not None and str(reference.year) not in raw:
            raise ValueError
        if reference.doi is not None and (
            _DOI.fullmatch(reference.doi) is None or reference.doi.casefold() not in raw.casefold()
        ):
            raise ValueError


def _validate_alignment(output, references, document):
    if not isinstance(output, CitationAlignmentOutput):
        raise ValueError
    expected = {reference.reference_id: reference for reference in references}
    checks = {check.reference_id: check for check in output.checks}
    if set(checks) != set(expected) or len(checks) != len(output.checks):
        raise ValueError
    for check in output.checks:
        if (check.status == "matched") != bool(check.citations):
            raise ValueError
        for citation in check.citations:
            if not any(
                citation.excerpt in text for text in _segment_text(citation.location, document)
            ):
                raise ValueError
            if any(
                all(
                    getattr(piece.location, key) == getattr(citation.location, key)
                    for key in _COORDINATES
                )
                and citation.excerpt in piece.excerpt
                for reference in references
                for piece in reference.evidence
            ):
                raise ValueError
    links = {link.finding_index: link for link in output.issue_links}
    if set(links) != set(range(len(output.findings))) or len(links) != len(output.issue_links):
        raise ValueError
    uncited = set()
    for index, finding in enumerate(output.findings):
        if (
            finding.category != "citation"
            or finding.severity not in {"important", "suggestion"}
            or finding.basis.spec_number is not None
            or finding.basis.excerpt is None
            or not any(
                finding.basis.excerpt in text for text in _segment_text(finding.location, document)
            )
        ):
            raise ValueError
        link = links[index]
        if link.reference_id is not None and link.reference_id not in expected:
            raise ValueError
        if link.kind == "missing_reference" and link.reference_id is not None:
            raise ValueError
        if link.kind == "uncited_reference":
            if link.reference_id is None:
                raise ValueError
            reference = expected[link.reference_id]
            if not any(
                all(
                    getattr(piece.location, key) == getattr(finding.location, key)
                    for key in _COORDINATES
                )
                and finding.basis.excerpt in piece.excerpt
                for piece in reference.evidence
            ):
                raise ValueError
            uncited.add(link.reference_id)
        elif any(
            all(
                getattr(piece.location, key) == getattr(finding.location, key)
                for key in _COORDINATES
            )
            and finding.basis.excerpt in piece.excerpt
            for reference in references
            for piece in reference.evidence
        ):
            # 正文缺引/支撑疑点不能用文献表本身充当论断或引用标记。
            raise ValueError
    if any((check.status == "uncited") != (number in uncited) for number, check in checks.items()):
        raise ValueError
    if not document.segments and output.coverage.checked:
        raise ValueError


def _database_finding(reference, verification, document_hash):
    data = {"reference": reference.model_dump(mode="json"), "verification": verification}
    input_hash = hashlib.sha256(
        _safe_json({"document_sha256": document_hash, **data}).encode("utf-8")
    ).hexdigest()
    return {
        "finding_id": str(uuid4()),
        "category": "citation",
        "severity": "blocking",
        "location": reference.evidence[0].location.model_dump(mode="json"),
        "basis": {"spec_number": None, "excerpt": reference.evidence[0].excerpt},
        "problem": "在本次已查询的Crossref和OpenAlex数据库中查无此条，尚不能确认该参考文献存在。",
        "suggestion": "核对作者、标题、年份和DOI；如数据库未收录，请向负责人提供可查证的出版信息。",
        "source": "program",
        "reviewer": "citation_database",
        "instruction_version": DATABASE_RULE_VERSION,
        "instruction_hash": hashlib.sha256(_DATABASE_RULE.encode("utf-8")).hexdigest(),
        "model": None,
        "call_id": None,
        "input_hash": input_hash,
        "document_sha256": document_hash,
        "reference_id": reference.reference_id,
        "verification": verification,
    }


def _query(reference):
    raw = _reference_text(reference)
    return CitationQuery(
        title=reference.title,
        authors=tuple(reference.authors),
        year=reference.year,
        doi=reference.doi,
        is_chinese=bool(re.search(r"[\u3400-\u9fff\U00020000-\U0003134f]", raw)),
    )


def _verification(value, query):
    if not isinstance(value, CitationVerification) or value.status not in {
        "verified",
        "not_found",
        "unverified",
    }:
        raise ValueError
    result = value.to_dict()
    _safe_json(result, maximum=64 * 1024)
    sources = result["sources"]
    if (
        len(sources) > 2
        or len({source["provider"] for source in sources}) != len(sources)
        or any(
            source["provider"] not in {"crossref", "openalex"}
            or source["status"] not in {"matched", "no_match", "unverified", "unavailable"}
            for source in sources
        )
    ):
        raise ValueError
    if value.status == "not_found" and (
        query.is_chinese
        or not (query.doi or (query.title and query.authors and query.year))
        or {source["provider"] for source in sources} != {"crossref", "openalex"}
        or any(source["status"] != "no_match" for source in sources)
    ):
        raise ValueError
    if value.status == "verified" and (
        not any(source["status"] == "matched" for source in sources)
        or any(source["status"] == "unverified" for source in sources)
    ):
        raise ValueError
    return result


def review_citations(
    client,
    *,
    project_id: UUID,
    job_id: UUID,
    document: ExtractedDocument,
    plan: dict,
    specification: dict,
    handoff_note: str,
    history: list[HistoricalOpinion | dict] | tuple[HistoricalOpinion | dict, ...],
    database_client,
) -> dict:
    """可信内部组件；数据库请求只含引文元数据，无交付/人工标注/终审状态写入。"""
    report = {
        "status": "incomplete",
        "extraction": None,
        "alignment": None,
        "references": [],
        "database_findings": [],
        "checks": [],
        "issue_links": [],
        "coverage": {"checked": [], "uncovered": []},
        "document_sha256": None,
        "extraction_warnings": [],
        "error_code": None,
    }
    try:
        if (
            not isinstance(project_id, UUID)
            or not isinstance(job_id, UUID)
            or not isinstance(document, ExtractedDocument)
            or document.format not in {"docx", "pptx", "pdf", "md"}
            or re.fullmatch(r"[a-f0-9]{64}", document.sha256) is None
            or not isinstance(plan, dict)
            or not isinstance(specification, dict)
            or not isinstance(handoff_note, str)
            or len(handoff_note) > 10000
            or "\0" in handoff_note
            or not isinstance(history, (list, tuple))
            or len(history) > 500
        ):
            raise ValueError
        historical = [
            HistoricalOpinion.model_validate_json(
                _safe_json(
                    value.model_dump(mode="json") if isinstance(value, HistoricalOpinion) else value
                )
            )
            for value in history
        ]
        context = {
            "plan": plan,
            "specification": specification,
            "delivery": {**document.to_dict(), "handoff_note": handoff_note},
        }
        _safe_json(
            {**context, "history": [record.model_dump(mode="json") for record in historical]}
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        report["error_code"] = "input_invalid"
        return report
    report["document_sha256"] = document.sha256
    report["extraction_warnings"] = [asdict(warning) for warning in document.warnings]
    extracted = []
    bibliography = []

    def validate_extraction(output):
        _validate_extraction(output, document)
        extracted[:] = output.references
        bibliography[:] = [output.bibliography_status]

    executor = ReviewerExecutor(client)
    extraction = executor.run(
        project_id=project_id,
        job_id=job_id,
        reviewer="citation_extract",
        instruction=load_citation_instruction("citation_extract"),
        context=context,
        history=historical,
        validate_output=validate_extraction,
    )
    report["extraction"] = extraction.to_dict()
    if extraction.status != "completed":
        report["error_code"] = "extraction_incomplete"
        return report
    report["bibliography_status"] = bibliography[0]
    if bibliography[0] == "unreadable":
        report["error_code"] = "source_unreadable"
        report["coverage"]["uncovered"].append("参考文献区域不可读，未完成文献核查。")
        return report
    cache, database_findings = {}, []
    database_error = None
    started = time.monotonic()
    for reference in extracted:
        query = _query(reference)
        key = (
            _cache_text(query.title or ""),
            tuple(_cache_text(author) for author in query.authors),
            query.year,
            (query.doi or "").casefold(),
            query.is_chinese,
        )
        if key not in cache:
            if (
                len(cache) >= MAX_DATABASE_QUERIES
                or time.monotonic() - started >= DATABASE_BUDGET_SECONDS
            ):
                database_error = "database_budget_exceeded"
                verification = {
                    "status": "unverified",
                    "reason": "本轮数据库查询预算已用尽。",
                    "sources": [],
                }
            else:
                try:
                    verification = _verification(database_client.verify(query), query)
                except QuotaExceededError:
                    raise
                except Exception:
                    verification = {
                        "status": "unverified",
                        "reason": "数据库核验未完成，请稍后重试。",
                        "sources": [],
                    }
                    database_error = database_error or "database_unavailable"
                if verification["status"] == "unverified" and any(
                    source["status"] == "unavailable" for source in verification["sources"]
                ):
                    database_error = database_error or "database_unavailable"
            cache[key] = verification
        verification = cache[key]
        source = {
            "reviewer": extraction.reviewer,
            "instruction_version": extraction.instruction_version,
            "model": extraction.model,
            "instruction_hash": extraction.instruction_hash,
            "input_hash": extraction.attempts[-1].input_hash,
            "call_id": str(extraction.attempts[-1].call_id),
        }
        report["references"].append(
            {
                **reference.model_dump(mode="json"),
                "raw_reference": _reference_text(reference),
                "verification": verification,
                "extraction_source": source,
            }
        )
        if verification["status"] == "not_found":
            database_findings.append(_database_finding(reference, verification, document.sha256))
    alignment_data = []

    def validate_alignment(output):
        _validate_alignment(output, extracted, document)
        alignment_data[:] = [output]

    alignment = executor.run(
        project_id=project_id,
        job_id=job_id,
        reviewer="citation_alignment",
        instruction=load_citation_instruction("citation_alignment"),
        context={
            **context,
            "delivery": {**context["delivery"], "references": report["references"]},
        },
        history=historical,
        validate_output=validate_alignment,
    )
    report["alignment"] = alignment.to_dict()
    for stage in (extraction, alignment):
        if stage.coverage:
            for kind in ("checked", "uncovered"):
                for text in getattr(stage.coverage, kind):
                    if len(report["coverage"][kind]) < 100 and text not in report["coverage"][kind]:
                        report["coverage"][kind].append(text)
    notices = []
    if document.warnings:
        notices.append("文本抽取存在未覆盖内容，请查看extraction_warnings逐项提示。")
    if bibliography[0] == "partial":
        notices.append("参考文献抽取不完整，仅核查已提取条目。")
    for reference in report["references"]:
        if reference["verification"]["status"] == "unverified":
            notices.append(f"{reference['reference_id']}：未能核验，不能视为不存在或已证实。")
        elif any(
            source["status"] == "unavailable" for source in reference["verification"]["sources"]
        ):
            notices.append(f"{reference['reference_id']}：已获得匹配，但未完成全部数据库查询。")
    # 平台提示优先保留在有界汇总；每条原始警告及逐文献结果另行完整保存。
    existing = report["coverage"]["uncovered"]
    report["coverage"]["uncovered"] = list(dict.fromkeys([*notices, *existing]))[:100]
    if alignment.status != "completed":
        report["error_code"] = "alignment_incomplete"
        return report
    if database_error:
        report["error_code"] = database_error
        return report
    report.update(
        status="completed",
        database_findings=database_findings,
        checks=[check.model_dump(mode="json") for check in alignment_data[0].checks],
        issue_links=[link.model_dump(mode="json") for link in alignment_data[0].issue_links],
    )
    return report
