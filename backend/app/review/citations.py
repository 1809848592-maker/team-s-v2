"""固定文献核查：有据抽取、只读数据库核对、引用对应意见，不作交付通过判断。"""

import hashlib
import re
import time
import unicodedata
from dataclasses import asdict
from uuid import UUID, uuid4

from app.review.citation_databases import CitationQuery, CitationVerification
from app.review.extract_types import ExtractedDocument, TextLocation
from app.review.reviewers.citation_types import CitationExcerpt, ExtractedReference
from app.review.reviewers.contract import (
    AdaptedOutput,
    adapt_findings,
    excerpt_matches,
    segment_inputs,
    segment_sources,
)
from app.review.reviewers.executor import ReviewerExecutor, _safe_json
from app.review.reviewers.instructions import load_citation_instruction
from app.review.reviewers.schemas import HistoricalOpinion
from app.services.llm_usage import QuotaExceededError

DATABASE_RULE_VERSION = "citation-database/2"
MAX_DATABASE_QUERIES = 100
DATABASE_BUDGET_SECONDS = 120
_DATABASE_RULE = (
    "在两个数据库充分查询均成功且无匹配时，仅对非中文文献报告本次数据库查无此条；"
    "中文未检出、弱匹配、元数据冲突及接口故障均未能核验，不证明全球不存在。"
    "语言判断仅忽略独立成行的文献表标题，文献标题、作者和条目中的汉字仍受保护。"
)
_COORDINATES = tuple(TextLocation.__dataclass_fields__)
_DOI = re.compile(r"10\.\d{4,9}/[A-Za-z0-9][A-Za-z0-9._;()/:+%-]{0,200}", re.I)
_BIBLIOGRAPHY_HEADINGS = frozenset({"参考文献", "参考文献:", "参考文献："})


def _normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _cache_text(value):
    # 验证摘录可忽略标点，缓存却不能合并re-sign与resign等不同检索键。
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _has_valid_coverage_notes(response):
    notes = response.get("coverage_notes")
    return isinstance(notes, list) and any(
        isinstance(note, str) and 0 < len(note.strip()) <= 1000 and "\0" not in note
        for note in notes[:100]
    )


def _reference_text(reference):
    return "\n".join(piece.excerpt for piece in reference.evidence)


def _metadata(raw):
    """只解析当前原文中明确的APA或GB/T结构；歧义保留为空。"""
    text = unicodedata.normalize("NFKC", raw).strip()
    text = "\n".join(
        line for line in text.splitlines() if line.strip() not in _BIBLIOGRAPHY_HEADINGS
    ).strip()
    text = re.sub(r"^\[\d+\]\s*", "", text)
    doi_match = _DOI.search(text)
    doi = doi_match.group().rstrip(".,;。") if doi_match else None
    title, authors, year = None, (), None
    apa = re.fullmatch(r"(.{1,400}?)\s*\((1[0-9]{3}|2[0-9]{3})[a-z]?\)\s*[.。]?\s*(.+)", text, re.S)
    if apa:
        author_text, raw_year, rest = apa.groups()
        # 标题与刊物有明确句号边界才提取题名，不把整条当成标题。
        title_match = re.match(r"(.{1,1000}?)(?:\.\s|。|$)", rest.strip())
        if title_match:
            title = title_match.group(1).strip().rstrip(".")
        authors = tuple(
            value.strip().rstrip(".")
            for value in re.split(r"\s+(?:&|and)\s+|;", author_text)
            if value.strip()
        )
        year = int(raw_year)
    else:
        # GB/T以文献类型标记明确分隔题名，避免在作者缩写处猜测句点。
        gb = re.match(r"(.{1,400}?)\.\s*(.{1,1000}?)\s*\[[A-Z](?:/[A-Z]+)?\](.*)", text, re.S)
        if gb:
            author_text, title, rest = gb.groups()
            authors = tuple(
                value.strip() for value in re.split(r"[,，;；]", author_text) if value.strip()
            )
            years = re.findall(r"(?<!\d)(?:1[0-9]{3}|2[0-9]{3})(?!\d)", rest)
            year = int(years[0]) if len(set(years)) == 1 else None
    # 不可靠或过长元数据不能触发“查无此条”。DOI仍仅来自原文。
    if (
        not title
        or len(title) > 1000
        or not authors
        or len(authors) > 20
        or any(len(value) > 1000 for value in authors)
    ):
        title, authors, year = None, (), None
    return {"title": title, "authors": authors, "year": year, "doi": doi}


def _extract_references(response, document):
    adapted = adapt_findings(
        response,
        category="citation",
        document=document,
        expected={"reference": {"available": bool(document.segments), "sources": set()}},
        sources={},
        severities=("suggestion",),
    )
    references, errors = [], list(adapted.errors)
    used = set()
    for index, finding in enumerate(adapted.findings):
        raw = finding.basis.excerpt
        identity = (finding.location.model_dump_json(), raw)
        if identity in used:
            errors.append({"path": f"findings.{index}", "code": "duplicate_reference"})
            continue
        used.add(identity)
        metadata = (
            _metadata(raw)
            if finding.basis.excerpt_verified
            else {
                "title": None,
                "authors": (),
                "year": None,
                "doi": None,
            }
        )
        references.append(
            ExtractedReference(
                reference_id=f"R{len(references) + 1}",
                evidence=(CitationExcerpt(location=finding.location, excerpt=raw),),
                excerpt_verified=finding.basis.excerpt_verified,
                **metadata,
            )
        )
    discarded = adapted.discarded_count + len(adapted.findings) - len(references)
    coverage = adapted.coverage
    if discarded > adapted.discarded_count:
        coverage = coverage.model_copy(
            update={
                "uncovered": tuple(
                    dict.fromkeys((*coverage.uncovered, f"{discarded} 条意见因格式无效未采用。"))
                )[:100],
            }
        )
    # 识别结果是平台内部元数据，不能作为引用问题意见汇入报告。
    output = AdaptedOutput(
        findings=(),
        coverage=coverage,
        errors=tuple(errors),
        discarded_count=discarded,
        incomplete=adapted.incomplete
        or (bool(adapted.findings) and not references and not _has_valid_coverage_notes(response)),
    )
    return output, references


def _adapt_alignment(response, references, document):
    """保留原引用判据：文献表不能冒充正文，R编号必须关联本轮文献。"""
    sources = {reference.reference_id: _reference_text(reference) for reference in references}
    segments = segment_sources(document)
    filtered, semantic_errors, original_indexes = [], [], []
    raw_findings = response.get("findings")
    if isinstance(raw_findings, list):
        for index, value in enumerate(raw_findings):
            invalid = False
            if (
                isinstance(value, dict)
                and isinstance(value.get("segment_id"), str)
                and value["segment_id"] in segments
            ):
                _, coordinates = segments[value["segment_id"]]
                related = [
                    reference
                    for reference in references
                    if any(
                        all(
                            getattr(piece.location, key) == coordinates.get(key)
                            for key in _COORDINATES
                        )
                        for piece in reference.evidence
                    )
                ]
                kind = value.get("check_key")
                excerpt = value.get("excerpt")
                context_key = value.get("context_key")
                if (
                    isinstance(kind, str)
                    and kind in {"missing_reference", "support_insufficient"}
                    and isinstance(excerpt, str)
                ):
                    invalid = any(
                        excerpt_matches(excerpt, _reference_text(reference))
                        for reference in related
                    )
                if kind == "missing_reference" and (
                    context_key is not None or value.get("context_excerpt") is not None
                ):
                    invalid = True
                if kind == "uncited_reference":
                    invalid = (
                        not isinstance(context_key, str)
                        or context_key not in sources
                        or not any(reference.reference_id == context_key for reference in related)
                    )
                    # 普通摘录未核对仍保留；已核对却引用了同片段另一文献才是错误关联。
                    if not invalid and isinstance(excerpt, str):
                        actual = segments[value["segment_id"]][0]
                        invalid = excerpt_matches(excerpt, actual) and not excerpt_matches(
                            excerpt, sources[context_key]
                        )
            if invalid:
                semantic_errors.append(
                    {"path": f"findings.{index}", "code": "citation_evidence_invalid"}
                )
            else:
                filtered.append(value)
                original_indexes.append(index)
    adapted = adapt_findings(
        {**response, "findings": filtered} if isinstance(raw_findings, list) else response,
        category="citation",
        document=document,
        expected={
            key: {"available": bool(document.segments), "sources": set(sources)}
            for key in (
                "missing_reference",
                "uncited_reference",
                "support_insufficient",
            )
        },
        sources=sources,
    )
    if not semantic_errors:
        return adapted
    discarded = adapted.discarded_count + len(semantic_errors)
    notes = tuple(
        dict.fromkeys((*adapted.coverage.uncovered, f"{discarded} 条意见因格式无效未采用。"))
    )[:100]
    errors = []
    for error in adapted.errors:
        match = re.match(r"findings\.(\d+)(.*)", error["path"])
        if match and int(match[1]) < len(original_indexes):
            error = {**error, "path": f"findings.{original_indexes[int(match[1])]}{match[2]}"}
        errors.append(error)
    return AdaptedOutput(
        findings=adapted.findings,
        coverage=adapted.coverage.model_copy(update={"uncovered": notes}),
        checks=adapted.checks,
        finding_links=adapted.finding_links,
        errors=(*errors, *semantic_errors),
        discarded_count=discarded,
        incomplete=adapted.incomplete
        or (not adapted.findings and not _has_valid_coverage_notes(response)),
    )


def _correspondence(references, document, links):
    result = []
    for reference in references:
        evidence_locations = {
            tuple(getattr(piece.location, key) for key in _COORDINATES)
            for candidate in references
            for piece in candidate.evidence
        }
        citations = []
        raw = _reference_text(reference)
        marker = re.match(r"\s*\[(\d+)\]", raw)
        surname = re.split(r"[,\s]", reference.authors[0])[0] if reference.authors else ""
        for segment in document.segments:
            if tuple(getattr(segment.location, key) for key in _COORDINATES) in evidence_locations:
                continue
            numeric = bool(marker and re.search(r"\[\s*" + marker[1] + r"\s*\]", segment.text))
            author_year = bool(
                surname
                and reference.year
                and surname.casefold() in segment.text.casefold()
                and str(reference.year) in segment.text
            )
            if numeric or author_year:
                citations.append(
                    {
                        "location": {
                            "description": "平台识别的正文引用",
                            **asdict(segment.location),
                        },
                        "excerpt": segment.text[:4000],
                    }
                )
        uncited = any(
            link["kind"] == "uncited_reference" and link["reference_id"] == reference.reference_id
            for link in links
        )
        result.append(
            {
                "reference_id": reference.reference_id,
                "status": "uncited" if uncited else "matched" if citations else "uncovered",
                "reason": "本条有未引用意见。"
                if uncited
                else "在有限可读正文中识别到编号或作者年份对应，不证明全文支撑。"
                if citations
                else "当前材料未能可靠建立正文引用对应。",
                "citations": [] if uncited else citations[:20],
            }
        )
    return result


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
    # 章节标题不说明条目语言；元数据不明时仍保守使用全部原文。
    language_text = (
        "\n".join(line for line in raw.splitlines() if line.strip() not in _BIBLIOGRAPHY_HEADINGS)
        if reference.title is not None
        else raw
    )
    language_text = "\n".join((language_text, reference.title or "", *reference.authors))
    return CitationQuery(
        title=reference.title,
        authors=tuple(reference.authors),
        year=reference.year,
        doi=reference.doi,
        is_chinese=bool(re.search(r"[\u3400-\u9fff\U00020000-\U0003134f]", language_text)),
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
            "delivery": {
                "sha256": document.sha256,
                "format": document.format,
                "segments": segment_inputs(document),
                "handoff_note": handoff_note,
            },
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

    def adapt_extraction(output):
        adapted, references = _extract_references(output, document)
        extracted[:] = references
        bibliography[:] = [
            "unreadable"
            if not document.segments
            else "partial"
            if adapted.errors
            else "complete"
            if references
            else "absent"
        ]
        return adapted

    executor = ReviewerExecutor(client)
    extraction = executor.run_compact(
        project_id=project_id,
        job_id=job_id,
        reviewer="citation_extract",
        instruction=load_citation_instruction("citation_extract"),
        context=context,
        history=historical,
        adapt_output=adapt_extraction,
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
        if not reference.excerpt_verified or not (
            query.doi or (query.title and query.authors and query.year)
        ):
            verification = {
                "status": "unverified",
                "reason": "原文摘录或元数据未能可靠核对，未作数据库否证。",
                "sources": [],
            }
        elif key not in cache:
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
        if (
            key in cache
            and reference.excerpt_verified
            and (query.doi or (query.title and query.authors and query.year))
        ):
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

    def adapt_alignment(output):
        adapted = _adapt_alignment(output, extracted, document)
        links = [
            {
                "finding_index": value["finding_index"],
                "kind": value["check_key"],
                "reference_id": value["context_refs"][0]["key"] if value["context_refs"] else None,
            }
            for value in adapted.finding_links
        ]
        alignment_data[:] = [(_correspondence(extracted, document, links), links)]
        return adapted

    alignment = executor.run_compact(
        project_id=project_id,
        job_id=job_id,
        reviewer="citation_alignment",
        instruction=load_citation_instruction("citation_alignment"),
        context={
            **context,
            "delivery": {
                **context["delivery"],
                "references": [
                    {
                        "reference_id": value["reference_id"],
                        "raw_reference": value["raw_reference"],
                        "verification": value["verification"],
                    }
                    for value in report["references"]
                ],
            },
        },
        history=historical,
        adapt_output=adapt_alignment,
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
        checks=alignment_data[0][0],
        issue_links=alignment_data[0][1],
    )
    return report
