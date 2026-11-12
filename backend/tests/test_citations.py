"""文献核查业务纯替身：真实片段、数据库否证据、对应关系与有限重试。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

import httpx2
import pytest

from app.review import citations
from app.review.citation_databases import (
    CitationDatabaseClient,
    CitationQuery,
    CitationVerification,
    ProviderEvidence,
)
from app.review.citations import review_citations
from app.review.extract_types import ExtractedDocument, ExtractionWarning, TextLocation, TextSegment
from app.review.llm import LLMRequestError
from app.review.model_config import load_model_config
from app.review.reviewers.schemas import ReviewerResult
from app.services.llm_usage import QuotaExceededError

PRIVATE = "PRIVATE_INVALID_CITATION_RESPONSE_NEVER_RETURN"
TITLE = "Robust causal inference"
DOI = "10.5555/example.001"
RAW = (
    "[1] Smith, J. (2020). Robust causal inference. Journal of Research. "
    "https://doi.org/10.5555/example.001"
)
BODY = "Smith (2020) explains associations; this result remains tentative."
CN_RAW = "[2] 王明. 团队协作研究[J]. 科研实践, 2021."


def location(line=5, **overrides):
    return {
        "description": f"Markdown第{line}行",
        "part": "markdown",
        "line_start": line,
        "line_end": line,
        **overrides,
    }


def document(*, raw=RAW, body=BODY, warnings=()):
    return ExtractedDocument(
        format="md",
        sha256=hashlib.sha256((body + "\n" + raw).encode()).hexdigest(),
        segments=(
            TextSegment(body, TextLocation(part="markdown", line_start=1, line_end=1)),
            TextSegment(raw, TextLocation(part="markdown", line_start=5, line_end=5)),
        ),
        warnings=tuple(warnings),
    )


def reference(*, excerpt=RAW, segment_id="S2", **overrides):
    return {
        "check_key": "reference",
        "severity": "suggestion",
        "segment_id": segment_id,
        "problem": "识别到参考文献条目。",
        "excerpt": excerpt,
        "suggestion": "由平台提取元数据并核对数据库。",
        **overrides,
    }


def extraction(*references, notes=()):
    return {"findings": list(references), "coverage_notes": list(notes)}


def opinion(*, kind="support_insufficient", excerpt=BODY, segment_id="S1", **overrides):
    return {
        "check_key": kind,
        "severity": "important",
        "segment_id": segment_id,
        "problem": "当前引用与所述内容的对应关系仍需核对。",
        "excerpt": excerpt,
        "suggestion": "人工核对当前原文和参考文献对应关系。",
        **overrides,
    }


def alignment(*findings, notes=()):
    return {"findings": list(findings), "coverage_notes": list(notes)}


def verification(status="verified", *, unavailable=False):
    source_status = (
        "unavailable"
        if unavailable
        else {"verified": "matched", "not_found": "no_match", "unverified": "unverified"}[status]
    )
    sources = tuple(
        ProviderEvidence(
            provider=provider,
            status=source_status,
            reason="固定替身检索结果。",
            title=TITLE if status == "verified" else None,
            authors=("Smith",) if status == "verified" else (),
            year=2020 if status == "verified" else None,
            doi=DOI if status == "verified" else None,
        )
        for provider in ("crossref", "openalex")
    )
    return CitationVerification(status=status, reason="固定替身核验结果。", sources=sources)


class FakeClient:
    def __init__(self, *responses):
        self.configuration = load_model_config()
        self.responses = list(responses)
        self.requests = []

    def complete_json(self, **request):
        self.requests.append(deepcopy(request))
        if not self.responses:
            raise AssertionError("文献审核模型调用超过测试约定")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class FakeDatabase:
    def __init__(self, *results):
        self.results = list(results) or [verification()]
        self.queries = []

    def verify(self, query):
        assert isinstance(query, CitationQuery)
        self.queries.append(query)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def review(client, database=None, **overrides):
    return review_citations(
        client,
        **{
            "project_id": uuid4(),
            "job_id": uuid4(),
            "document": document(),
            "plan": {"argument": "论证应分清相关和因果。"},
            "specification": {"items": []},
            "handoff_note": "交接数据来源见参考文献。",
            "history": [],
            "database_client": database if database is not None else FakeDatabase(),
            **overrides,
        },
    )


def assert_incomplete(report):
    assert report["status"] == "incomplete"
    assert report["database_findings"] == []
    assert report["checks"] == []
    assert report["issue_links"] == []
    assert PRIVATE not in json.dumps(report, ensure_ascii=False)


def request_hash(request):
    return hashlib.sha256(
        json.dumps(request["messages"], ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


@pytest.fixture
def empty_citation_databases():
    requests, queries = [], []

    def empty_results(request):
        requests.append(request)
        assert request.method == "GET"
        if request.url.path != "/works":
            return httpx2.Response(404, json={"error": "not found"})
        assert "authorization" not in request.headers
        if request.url.host == "api.crossref.org":
            body = {
                "status": "ok",
                "message-type": "work-list",
                "message": {"items": [], "total-results": 0},
            }
        else:
            assert request.url.host == "api.openalex.org"
            body = {"results": [], "meta": {"count": 0}}
        return httpx2.Response(200, json=body)

    class RecordingDatabase(CitationDatabaseClient):
        def verify(self, query):
            queries.append(query)
            return super().verify(query)

    return (
        RecordingDatabase(transport=httpx2.MockTransport(empty_results)),
        queries,
        requests,
    )


def test_platform_extracts_metadata_maps_coordinates_and_preserves_stage_sources():
    client, database = FakeClient(extraction(reference()), alignment(opinion())), FakeDatabase()
    doc = document()
    before = asdict(doc)
    result = review(client, database, document=doc)
    assert result["status"] == "completed" and asdict(doc) == before
    assert len(client.requests) == 2 and len(database.queries) == 1
    query = database.queries[0]
    assert query.title == TITLE and query.authors == ("Smith, J",) and query.year == 2020
    assert query.doi == DOI and query.is_chinese is False
    saved = result["references"][0]
    assert saved["reference_id"] == "R1" and saved["raw_reference"] == RAW
    assert saved["evidence"][0]["location"]["line_start"] == 5
    assert saved["excerpt_verified"] is True
    assert saved["verification"]["status"] == "verified"
    assert result["extraction"]["findings"] == []
    assert result["checks"][0]["status"] == "matched"
    assert result["alignment"]["findings"][0]["severity"] == "important"
    for index, stage in enumerate(("extraction", "alignment")):
        value = result[stage]
        assert value["attempts"][0]["input_hash"] == request_hash(client.requests[index])
        ReviewerResult.model_validate_json(json.dumps(value, ensure_ascii=False))
    assert result["issue_links"] == [
        {"finding_index": 0, "kind": "support_insufficient", "reference_id": None}
    ]
    context = "\n".join(message["content"] for message in client.requests[0]["messages"])
    assert '"segment_id": "S2"' in context and '"line_start"' not in context
    assert BODY not in repr(database.queries)


@pytest.mark.parametrize(
    "changes",
    [
        {"segment_id": "S999"},
        {"segment_id": "BAD"},
        {"check_key": "unknown"},
        {"severity": "blocking"},
        {"excerpt": ""},
        {"title": "invented title"},
        {"reference_id": "R999"},
        {"bibliography_status": "complete"},
    ],
)
def test_invalid_extracted_sibling_does_not_erase_valid_reference_or_retry(changes):
    client = FakeClient(extraction(reference(), reference(**changes)), alignment())
    result = review(client)
    assert result["status"] == "completed" and len(client.requests) == 2
    assert len(result["references"]) == 1 and result["bibliography_status"] == "partial"
    assert any("1 条意见因格式无效未采用" in note for note in result["coverage"]["uncovered"])


def test_duplicate_reference_does_not_double_query_or_overwrite_metadata():
    result = review(FakeClient(extraction(reference(), reference()), alignment()))
    assert result["status"] == "completed" and len(result["references"]) == 1
    assert result["references"][0]["reference_id"] == "R1"


def test_unverified_excerpt_is_retained_as_unverified_without_database_negation():
    database = FakeDatabase(verification("not_found"))
    result = review(
        FakeClient(extraction(reference(excerpt="Smith (2020). Invented paper.")), alignment()),
        database,
    )
    assert result["status"] == "completed"
    assert result["references"][0]["excerpt_verified"] is False
    assert result["references"][0]["verification"]["status"] == "unverified"
    assert result["database_findings"] == [] and database.queries == []
    assert any("摘录未能核对" in note for note in result["coverage"]["uncovered"])


def test_normalized_excerpt_matches_while_original_evidence_is_preserved():
    normalized = RAW.replace("(2020)", "（2020）").replace("Research", "Research  ")
    result = review(FakeClient(extraction(reference(excerpt=normalized)), alignment()))
    assert result["references"][0]["excerpt_verified"] is True
    assert result["references"][0]["raw_reference"] == normalized


@pytest.mark.parametrize(
    "raw, title, author, year",
    [
        ("Smith (2020). Research methods. Journal.", "Research methods", "Smith", 2020),
        ("[2] 王明. 团队协作研究[J]. 科研实践, 2021.", "团队协作研究", "王明", 2021),
        ("[3] Smith J. Causal estimation[J]. Journal, 2022.", "Causal estimation", "Smith J", 2022),
    ],
)
def test_metadata_comes_from_explicit_apa_or_gbt_excerpt(raw, title, author, year):
    database = FakeDatabase()
    result = review(
        FakeClient(extraction(reference(excerpt=raw)), alignment()),
        database,
        document=document(raw=raw),
    )
    assert result["status"] == "completed"
    assert database.queries[0].title == title and database.queries[0].authors == (author,)
    assert database.queries[0].year == year


@pytest.mark.parametrize(
    "raw",
    [
        "不完整的参考条目",
        "Smith. A title without an unambiguous year.",
        "[1] A missing title and unknown authors 2020",
        "A title doi:invalid",
    ],
)
def test_ambiguous_metadata_is_unverified_and_never_supplied_by_model_problem(raw):
    database = FakeDatabase(verification("not_found"))
    value = reference(excerpt=raw, problem='{"title":"invented","authors":["Smith"],"year":2020}')
    result = review(
        FakeClient(extraction(value), alignment()), database, document=document(raw=raw)
    )
    assert result["status"] == "completed" and database.queries == []
    saved = result["references"][0]
    assert saved["title"] is None and saved["authors"] == [] and saved["year"] is None
    assert saved["verification"]["status"] == "unverified" and result["database_findings"] == []


def test_complete_two_database_negation_produces_program_blocking_only():
    result = review(
        FakeClient(extraction(reference()), alignment()), FakeDatabase(verification("not_found"))
    )
    assert result["status"] == "completed"
    finding = result["database_findings"][0]
    assert finding["severity"] == "blocking" and finding["source"] == "program"
    assert finding["instruction_version"] == "citation-database/2"
    assert finding["call_id"] is None and finding["reference_id"] == "R1"


@pytest.mark.parametrize("heading", ["参考文献", "参考文献:", "参考文献：", "  参考文献：  "])
def test_chinese_bibliography_heading_does_not_reclassify_english_reference(
    heading, empty_citation_databases
):
    database, queries, requests = empty_citation_databases
    raw = heading + "\n" + RAW
    result = review(
        FakeClient(extraction(reference(excerpt=raw)), alignment()),
        database,
        document=document(raw=raw),
    )
    assert result["status"] == "completed" and queries[0].is_chinese is False
    assert len(requests) == 4  # DOI detail miss plus complete metadata searches in both providers.
    assert result["references"][0]["verification"]["status"] == "not_found"
    assert result["database_findings"][0]["severity"] == "blocking"


@pytest.mark.parametrize(
    "raw",
    [
        CN_RAW,
        "参考文献\n" + CN_RAW,
        RAW.replace("Smith", "王明"),
        RAW + "\n科研实践",
    ],
)
def test_real_chinese_content_remains_unverified_on_no_match(raw, empty_citation_databases):
    database, queries, _ = empty_citation_databases
    result = review(
        FakeClient(extraction(reference(excerpt=raw)), alignment()),
        database,
        document=document(raw=raw),
    )
    assert result["status"] == "completed" and queries[0].is_chinese is True
    assert result["references"][0]["verification"]["status"] == "unverified"
    assert result["database_findings"] == []


@pytest.mark.parametrize(
    "negative",
    [
        CitationVerification(
            "not_found", "只有一个库否证", (ProviderEvidence("crossref", "no_match", "无匹配"),)
        ),
        CitationVerification(
            "not_found",
            "有一库故障",
            (
                ProviderEvidence("crossref", "no_match", "无匹配"),
                ProviderEvidence("openalex", "unavailable", "故障"),
            ),
        ),
        CitationVerification(
            "verified", "没有匹配证据", (ProviderEvidence("crossref", "no_match", "无匹配"),)
        ),
    ],
)
def test_uncertain_or_conflicting_database_result_cannot_become_blocking(negative):
    result = review(FakeClient(extraction(reference()), alignment()), FakeDatabase(negative))
    assert_incomplete(result)
    assert result["error_code"] == "database_unavailable"


def test_injected_chinese_not_found_is_rejected():
    result = review(
        FakeClient(extraction(reference(excerpt=CN_RAW)), alignment()),
        FakeDatabase(verification("not_found")),
        document=document(raw=CN_RAW),
    )
    assert_incomplete(result)
    assert result["references"][0]["verification"]["status"] == "unverified"


@pytest.mark.parametrize(
    "failure", [RuntimeError(PRIVATE), verification("unverified", unavailable=True)]
)
def test_database_failure_is_incomplete_without_leaking_details_or_retry(failure):
    database = FakeDatabase(failure)
    result = review(FakeClient(extraction(reference()), alignment()), database)
    assert_incomplete(result)
    assert len(database.queries) == 1


def test_metadata_cache_reuses_lookup_for_distinct_reference_locations():
    doc = document()
    doc = replace(
        doc,
        segments=(
            *doc.segments,
            TextSegment(RAW, TextLocation(part="markdown", line_start=8, line_end=8)),
        ),
    )
    database = FakeDatabase()
    result = review(
        FakeClient(extraction(reference(), reference(segment_id="S3")), alignment()),
        database,
        document=doc,
    )
    assert len(database.queries) == 1 and len(result["references"]) == 2
    assert [r["reference_id"] for r in result["references"]] == ["R1", "R2"]


def test_database_cache_does_not_merge_re_sign_and_resign_titles():
    raw1, raw2 = "Smith (2020). re-sign. Journal.", "Smith (2020). resign. Journal."
    doc = document(raw=raw1)
    doc = replace(
        doc,
        segments=(
            *doc.segments,
            TextSegment(raw2, TextLocation(part="markdown", line_start=8, line_end=8)),
        ),
    )
    database = FakeDatabase()
    result = review(
        FakeClient(
            extraction(reference(excerpt=raw1), reference(excerpt=raw2, segment_id="S3")),
            alignment(),
        ),
        database,
        document=doc,
    )
    assert result["status"] == "completed" and len(database.queries) == 2


def test_exhausted_database_time_budget_stops_before_lookup(monkeypatch):
    ticks = iter([0, citations.DATABASE_BUDGET_SECONDS + 1])
    monkeypatch.setattr(citations.time, "monotonic", lambda: next(ticks))
    database = FakeDatabase()
    result = review(FakeClient(extraction(reference()), alignment()), database)
    assert_incomplete(result)
    assert result["error_code"] == "database_budget_exceeded" and database.queries == []


@pytest.mark.parametrize("kind", ["missing_reference", "uncited_reference", "support_insufficient"])
def test_alignment_kinds_are_program_linked_without_cross_array_model_fields(kind):
    changes = (
        {"segment_id": "S2", "excerpt": RAW, "context_key": "R1", "context_excerpt": RAW}
        if kind == "uncited_reference"
        else {}
    )
    result = review(FakeClient(extraction(reference()), alignment(opinion(kind=kind, **changes))))
    assert result["status"] == "completed"
    link = result["issue_links"][0]
    assert link["kind"] == kind and link["finding_index"] == 0
    assert link["reference_id"] == ("R1" if kind == "uncited_reference" else None)
    if kind == "uncited_reference":
        assert result["checks"][0]["status"] == "uncited"


@pytest.mark.parametrize(
    "changes",
    [
        {"severity": "blocking"},
        {"segment_id": "S999"},
        {"check_key": "unknown"},
        {"problem": ""},
        {"finding_index": 0},
        {"segment_id": []},
        {"check_key": []},
        {"context_key": []},
    ],
)
def test_invalid_alignment_sibling_does_not_erase_valid_advisory(changes):
    client = FakeClient(extraction(reference()), alignment(opinion(), opinion(**changes)))
    result = review(client)
    assert result["status"] == "completed" and len(result["alignment"]["findings"]) == 1
    assert len(client.requests) == 2


@pytest.mark.parametrize("kind", ["missing_reference", "support_insufficient"])
def test_body_issue_cannot_use_bibliography_as_body_evidence_but_valid_sibling_survives(kind):
    invalid = opinion(kind=kind, segment_id="S2", excerpt=RAW)
    result = review(FakeClient(extraction(reference()), alignment(opinion(), invalid)))
    assert result["status"] == "completed"
    assert len(result["alignment"]["findings"]) == 1
    assert any("1 条意见因格式无效未采用" in note for note in result["coverage"]["uncovered"])


@pytest.mark.parametrize(
    "changes",
    [
        {"context_key": "R999", "context_excerpt": RAW, "segment_id": "S2", "excerpt": RAW},
        {"context_key": "R1", "context_excerpt": RAW, "segment_id": "S1"},
    ],
)
def test_uncited_issue_requires_current_reference_and_actual_reference_segment(changes):
    result = review(
        FakeClient(extraction(reference()), alignment(opinion(kind="uncited_reference", **changes)))
    )
    assert_incomplete(result)
    assert result["error_code"] == "alignment_incomplete"


@pytest.mark.parametrize(
    "notes",
    [
        [" "],
        ["x" * 1001],
        [1],
        [None],
        [False],
        [{}],
        ["\0"],
        [" "] * 100 + ["超出范围的说明"],
    ],
)
def test_all_semantically_invalid_findings_with_invalid_notes_are_incomplete(notes):
    invalid = opinion(kind="support_insufficient", segment_id="S2", excerpt=RAW)
    result = review(FakeClient(extraction(reference()), alignment(invalid, notes=notes)))
    assert_incomplete(result)
    assert result["alignment"]["status"] == "incomplete"
    assert result["error_code"] == "alignment_incomplete"


def test_all_semantically_invalid_findings_with_valid_note_preserve_uncovered_completion():
    invalid = opinion(kind="support_insufficient", segment_id="S2", excerpt=RAW)
    result = review(
        FakeClient(extraction(reference()), alignment(invalid, notes=["未能可靠检查正文。"]))
    )
    assert result["status"] == "completed"
    assert result["alignment"]["findings"] == []
    assert "未能可靠检查正文。" in result["alignment"]["coverage"]["uncovered"]


@pytest.mark.parametrize(
    "context",
    [
        {"context_key": "R1", "context_excerpt": RAW},
        {"context_key": "R999", "context_excerpt": RAW},
        {"context_key": "R1"},
        {"context_excerpt": RAW},
    ],
)
def test_missing_reference_cannot_link_existing_or_unknown_reference_but_valid_sibling_survives(
    context,
):
    invalid = opinion(kind="missing_reference", **context)
    client = FakeClient(extraction(reference()), alignment(opinion(), invalid))
    result = review(client)
    assert result["status"] == "completed" and len(client.requests) == 2
    assert len(result["alignment"]["findings"]) == 1
    assert result["issue_links"] == [
        {"finding_index": 0, "kind": "support_insufficient", "reference_id": None}
    ]
    assert any("1 条意见因格式无效未采用" in note for note in result["coverage"]["uncovered"])


def test_alignment_unmatched_excerpt_retains_opinion_with_flag():
    result = review(FakeClient(extraction(reference()), alignment(opinion(excerpt="未出现的论断"))))
    assert result["status"] == "completed"
    assert result["alignment"]["findings"][0]["basis"]["excerpt_verified"] is False


def test_absent_bibliography_still_checks_body_missing_reference():
    result = review(FakeClient(extraction(), alignment(opinion(kind="missing_reference"))))
    assert result["status"] == "completed" and result["bibliography_status"] == "absent"
    assert result["references"] == [] and result["checks"] == []
    assert result["alignment"]["findings"]


def test_no_readable_segments_never_becomes_absent_or_pass():
    database = FakeDatabase()
    client = FakeClient(extraction(notes=("没有可读片段，无法识别。",)))
    result = review(client, database, document=replace(document(), segments=()))
    assert_incomplete(result)
    assert result["error_code"] == "source_unreadable"
    assert len(client.requests) == 1 and database.queries == []


def test_failed_alignment_keeps_component_incomplete_and_discards_program_blocking():
    client = FakeClient(extraction(reference()), alignment(opinion(segment_id="S999")))
    result = review(client, FakeDatabase(verification("not_found")))
    assert_incomplete(result)
    assert result["error_code"] == "alignment_incomplete" and len(client.requests) == 2


@pytest.mark.parametrize("stage", ["extraction", "alignment"])
@pytest.mark.parametrize("code", ["timeout", "connection", "provider_error"])
def test_provider_failure_stops_stage_without_business_retry(stage, code):
    values = (
        [LLMRequestError(code)]
        if stage == "extraction"
        else [extraction(reference()), LLMRequestError(code)]
    )
    client = FakeClient(*values)
    result = review(client)
    assert_incomplete(result)
    assert len(client.requests) == (1 if stage == "extraction" else 2)


@pytest.mark.parametrize("stage", ["extraction", "alignment", "database"])
def test_quota_error_propagates_in_each_stage(stage):
    quota = QuotaExceededError("platform", None)
    client = (
        FakeClient(quota)
        if stage == "extraction"
        else FakeClient(extraction(reference()), quota if stage == "alignment" else alignment())
    )
    database = FakeDatabase(quota) if stage == "database" else FakeDatabase()
    with pytest.raises(QuotaExceededError):
        review(client, database)


def test_all_extraction_warnings_survive_bounded_stage_coverage():
    warnings = tuple(ExtractionWarning(f"unread_{i}", "该部分未读取。") for i in range(101))
    result = review(
        FakeClient(extraction(reference()), alignment()), document=document(warnings=warnings)
    )
    assert result["extraction_warnings"] == [asdict(value) for value in warnings]
    for stage in ("extraction", "alignment"):
        ReviewerResult.model_validate_json(json.dumps(result[stage], ensure_ascii=False))


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "not-a-uuid"},
        {"job_id": True},
        {"document": None},
        {"document": {}},
        {"plan": None},
        {"specification": []},
        {"handoff_note": None},
        {"handoff_note": "x" * 10001},
        {"history": None},
        {"history": [{"unexpected": PRIVATE}]},
    ],
)
def test_invalid_review_input_never_calls_model_or_databases(overrides):
    client, database = FakeClient(), FakeDatabase()
    assert_incomplete(review(client, database, **overrides))
    assert client.requests == [] and database.queries == []
