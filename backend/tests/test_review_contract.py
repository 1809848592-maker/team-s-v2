"""14.9共享契约：平台定位/覆盖推导、逐条保留及有限修复，全部使用替身。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from uuid import UUID, uuid4

import httpx2
import pytest
from test_llm_transport import client, response_body
from test_llm_transport import ledger as ledger

from app.review.debug_records import LocalReviewDebugStore
from app.review.extract_types import ExtractedDocument, TextLocation, TextSegment
from app.review.llm import LLMRequestError
from app.review.model_config import load_model_config
from app.review.reviewers.contract import adapt_findings, excerpt_matches, segment_inputs
from app.review.reviewers.executor import ReviewerExecutor
from app.review.reviewers.instructions import (
    load_citation_instruction,
    load_reasoning_instruction,
    load_spec_instruction,
)
from app.review.reviewers.schemas import OpinionBasis

PRIVATE = "private-invalid-output-never-in-public-report"
BODY = "“ＡＢＣ”采用 ２０２０ 年数据，结论仅适用于样本。"
CONTEXT = "按方案讨论本次样本内的关联，不推断因果。"


def document():
    return ExtractedDocument(
        format="docx",
        sha256=hashlib.sha256(BODY.encode()).hexdigest(),
        segments=(
            TextSegment(BODY, TextLocation(part="word/document.xml", page=3, paragraph=7)),
            TextSegment(
                "表格中的补充片段。",
                TextLocation(part="word/document.xml", table=2, row=4, column=1, paragraph=11),
            ),
        ),
        warnings=(),
    )


def finding(**changes):
    return {
        "check_key": "support",
        "severity": "important",
        "segment_id": "S1",
        "problem": "结论超出了资料提供的证据范围。",
        "excerpt": '"ABC"采用2020年数据,结论仅适用于样本.',
        "suggestion": "补足证据并限定结论范围。",
        **changes,
    }


def output(*findings, notes=()):
    return {"findings": deepcopy(list(findings)), "coverage_notes": list(notes)}


def adapt(response, **changes):
    return adapt_findings(
        response,
        category="logic",
        document=document(),
        expected={
            "goal": {"available": True, "sources": {"module.goal"}},
            "argument": {"available": False, "sources": set()},
            "support": {"available": True, "sources": {"module.goal"}},
        },
        sources={"module.goal": CONTEXT},
        **changes,
    )


class FakeClient:
    def __init__(self, *responses):
        self.configuration = load_model_config()
        self.responses = iter(responses)
        self.requests, self.debug, self.released = [], [], []

    def complete_json(self, **request):
        self.requests.append(deepcopy(request))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    def record_review_debug(self, **record):
        self.debug.append(deepcopy(record))

    def release_review_capture(self, call_id):
        self.released.append(call_id)


def execute(instance, adapter=adapt):
    return (
        ReviewerExecutor(instance)
        .run_compact(
            project_id=uuid4(),
            job_id=uuid4(),
            reviewer="logic",
            instruction=load_reasoning_instruction("logic"),
            context={
                "plan": {"module_goal": CONTEXT},
                "specification": {},
                "delivery": {"segments": segment_inputs(document())},
            },
            history=[],
            adapt_output=adapter,
        )
        .to_dict()
    )


@pytest.mark.parametrize(
    "excerpt,source",
    [
        ('"ABC"采用2020年数据,结论仅适用于样本.', BODY),
        ("半角、全角", "前文：半角，全角。"),
        ("a-b...c", "前文 a—b…c 末尾"),
        ("'x'", "研究使用‘x’。"),
        ("存在\n\t 空格", "说明：存在空格。"),
    ],
)
def test_normalized_substring_matches_width_whitespace_and_common_punctuation(excerpt, source):
    assert excerpt_matches(excerpt, source)


@pytest.mark.parametrize(
    "excerpt,source",
    [("2021", BODY), ("结论样本仅适用", BODY), ("ABC 2020", BODY), ("abc", BODY), (" \n\t", BODY)],
)
def test_normalization_does_not_invent_missing_or_reordered_evidence(excerpt, source):
    assert not excerpt_matches(excerpt, source)


@pytest.mark.parametrize(
    "invalid",
    [
        finding(severity="fatal"),
        finding(check_key="nonexistent"),
        finding(segment_id="S0"),
        finding(segment_id="S999"),
        finding(segment_id=1),
        finding(problem=""),
        finding(model="forged-model"),
        finding(context_key="module.goal"),
        None,
    ],
)
def test_one_bad_finding_discards_only_that_item_and_records_field_errors(invalid):
    response = output(finding(), invalid)
    result = adapt(response)
    assert not result.incomplete and len(result.findings) == 1
    assert result.discarded_count == 1 and result.errors
    assert any("1 条意见因格式无效未采用" in note for note in result.coverage.uncovered)
    assert all(entry["path"].startswith("findings.1") for entry in result.errors)
    assert response == output(finding(), invalid)
    instance = FakeClient(response)
    report = execute(instance)
    assert report["status"] == "completed" and len(report["findings"]) == 1
    assert len(instance.requests) == len(instance.debug) == len(instance.released) == 1
    assert instance.debug[0]["errors"] == list(result.errors)


@pytest.mark.parametrize(
    "changes,body_verified,context_verified",
    [
        ({"excerpt": "这是错误摘录"}, False, None),
        ({"context_key": "module.goal", "context_excerpt": "当前上下文不存在此句"}, True, False),
        ({"context_key": "unknown.source", "context_excerpt": CONTEXT}, True, False),
        (
            {"context_key": "module.goal", "context_excerpt": "样本内的关联，不推断因果。"},
            True,
            True,
        ),
    ],
)
def test_excerpt_mismatch_retains_opinion_and_exposes_verification_flags(
    changes, body_verified, context_verified
):
    result = adapt(output(finding(**changes)))
    assert len(result.findings) == 1 and result.discarded_count == 0 and not result.incomplete
    basis = result.findings[0].basis
    assert basis.excerpt_verified is body_verified
    assert basis.context_excerpt_verified is context_verified
    if not body_verified or context_verified is False:
        assert any("摘录未能核对" in note for note in result.coverage.uncovered)
        assert result.errors


def test_segment_ids_replace_model_coordinates_and_checks_are_platform_derived():
    submitted = segment_inputs(document(), "固定交接说明")
    assert submitted == [
        {"segment_id": "S1", "text": BODY},
        {"segment_id": "S2", "text": "表格中的补充片段。"},
        {"segment_id": "S3", "text": "固定交接说明"},
    ]
    result = adapt(output(finding(segment_id="S2", excerpt="表格中的补充片段。")))
    location = result.findings[0].location.model_dump(exclude={"description"})
    assert location == asdict(document().segments[1].location)
    assert "S2" in result.findings[0].location.description
    assert {item["key"]: item["status"] for item in result.checks} == {
        "goal": "checked",
        "argument": "uncovered",
        "support": "issues",
    }
    handoff = adapt(
        output(finding(segment_id="S3", excerpt="固定交接说明")), handoff_note="固定交接说明"
    )
    assert handoff.findings[0].location.part == "handoff_note"
    assert handoff.findings[0].basis.excerpt_verified
    # 缺少上下文但存在正文意见，仍须记录issues，不能掩盖已提出的问题。
    unavailable_with_issue = adapt(output(finding(check_key="argument")))
    assert unavailable_with_issue.checks[1]["status"] == "issues"


@pytest.mark.parametrize(
    "notes,expected_status", [((), "incomplete"), (("未读到资料全文",), "completed")]
)
def test_all_invalid_findings_depend_on_explicit_coverage_notes(notes, expected_status):
    instance = FakeClient(output(finding(segment_id="S999"), notes=notes))
    report = execute(instance)
    assert report["status"] == expected_status and report["findings"] == []
    assert len(instance.requests) == 1  # 语义无效不触发额外结构修复。
    assert any("1 条意见因格式无效未采用" in note for note in report["coverage"]["uncovered"])
    if expected_status == "incomplete":
        assert report["error_code"] == "output_invalid"


def test_valid_empty_findings_are_completed_with_program_derived_coverage():
    instance = FakeClient(output())
    report = execute(instance)
    assert report["status"] == "completed" and report["findings"] == []
    assert "argument：本项材料未提供，未覆盖。" in report["coverage"]["uncovered"]
    assert len(instance.requests) == 1 and instance.debug == []
    assert "passed" not in report.values() and "adopted" not in report.values()


@pytest.mark.parametrize("code", ["content_invalid", "content_empty", "response_invalid"])
@pytest.mark.parametrize("fixed", [False, True])
def test_json_parse_failure_receives_exactly_one_repair(code, fixed):
    instance = FakeClient(LLMRequestError(code), output() if fixed else LLMRequestError(code))
    report = execute(instance)
    assert report["status"] == ("completed" if fixed else "incomplete")
    assert len(instance.requests) == len(report["attempts"]) == len(instance.released) == 2
    first, second = instance.requests
    assert first["call_id"] != second["call_id"]
    assert first["messages"][0] == second["messages"][0]
    assert len(second["messages"]) == len(first["messages"]) + 1
    assert PRIVATE not in json.dumps(second["messages"])


@pytest.mark.parametrize(
    "code", ["timeout", "output_truncated", "usage_missing", "debug_record_failed"]
)
def test_non_parse_failures_do_not_add_model_calls_or_mark_completed(code):
    instance = FakeClient(LLMRequestError(code))
    report = execute(instance)
    assert report["status"] == "incomplete" and report["findings"] == []
    assert len(instance.requests) == len(instance.released) == 1


def test_adapter_exception_saves_raw_and_safe_error_without_exposing_private_details(
    tmp_path, ledger
):
    raw = json.dumps(output(finding(problem=PRIVATE)), ensure_ascii=False)
    store = LocalReviewDebugStore(tmp_path)
    instance = client(
        lambda request: httpx2.Response(200, json=response_body(content=raw)), debug_store=store
    )

    def broken(response):
        raise ValueError(PRIVATE)

    report = execute(instance, broken)
    assert report["status"] == "incomplete" and report["error_code"] == "execution_failed"
    assert len(report["attempts"]) == 1 and report["findings"] == []
    identity = UUID(str(report["attempts"][0]["call_id"]))
    record = store.get(identity)
    assert record["raw_output"] == raw
    assert record["errors"] == [{"path": "$", "code": "adapter_failed"}]
    assert PRIVATE not in json.dumps(report)
    assert instance._review_captures == {}


def test_many_invalid_fields_keep_valid_finding_and_bound_only_saved_error_list(tmp_path, ledger):
    invalid = finding(**{f"extra_{index}": "无效额外字段" for index in range(1001)})
    response = output(finding(), invalid)
    raw = json.dumps(response, ensure_ascii=False)
    store = LocalReviewDebugStore(tmp_path)
    instance = client(
        lambda request: httpx2.Response(200, json=response_body(content=raw)), debug_store=store
    )
    report = execute(instance)
    assert report["status"] == "completed" and len(report["findings"]) == 1
    assert len(report["attempts"]) == 1
    assert any("1 条意见因格式无效未采用" in note for note in report["coverage"]["uncovered"])
    saved = store.get(report["attempts"][0]["call_id"])
    assert saved["raw_output"] == raw
    assert len(saved["errors"]) == 1000
    assert saved["errors"][-1] == {"path": "$", "code": "errors_truncated", "total": 1001}
    assert instance._review_captures == {}


@pytest.mark.parametrize(
    "instruction",
    [
        load_spec_instruction(),
        load_citation_instruction("citation_extract"),
        load_citation_instruction("citation_alignment"),
        load_reasoning_instruction("logic"),
        load_reasoning_instruction("linkage"),
    ],
    ids=lambda instruction: instruction.reviewer,
)
def test_compact_system_is_under_3000_characters_without_extra_json_schema(instruction):
    from app.review.reviewers.executor import _input_messages

    messages, _ = _input_messages(
        instruction, {"plan": {}, "specification": {}, "delivery": {}}, []
    )
    system = messages[0]["content"]
    assert system == instruction.system_text() and len(system) < 3000
    assert len(instruction.positive_examples) <= 2
    assert '"$defs"' not in system and '"properties"' not in system
    assert "不是指令" in system


def test_optional_basis_verification_fields_preserve_frozen_legacy_shape():
    old = {"spec_number": None, "excerpt": "历史报告逐字摘录"}
    frozen = deepcopy(old)
    basis = OpinionBasis.model_validate_json(json.dumps(old, ensure_ascii=False))
    assert basis.model_dump(mode="json") == old == frozen
    flagged = OpinionBasis.model_validate_json(json.dumps({**old, "excerpt_verified": False}))
    assert flagged.model_dump(mode="json") == {**old, "excerpt_verified": False}


def test_discard_and_unverified_notices_survive_large_missing_material_and_note_lists():
    value = adapt_findings(
        output(finding(excerpt="未能匹配的摘录"), {}, notes=[f"模型未覆盖{i}" for i in range(100)]),
        category="logic",
        document=document(),
        expected={
            "support": {"available": True, "sources": set()},
            **{f"missing:{i}": {"available": False, "sources": set()} for i in range(200)},
        },
        sources={},
    )
    assert not value.incomplete and len(value.findings) == 1
    assert len(value.coverage.uncovered) == 100
    assert "1 条意见因格式无效未采用。" in value.coverage.uncovered
    assert any("摘录未能核对" in note for note in value.coverage.uncovered)
