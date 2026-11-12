"""规范审核业务纯替身：逐条覆盖、真实依据和位置、有限语义修复。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

import pytest

from app.review.extract_types import (
    ExtractedDocument,
    ExtractionWarning,
    TextLocation,
    TextSegment,
)
from app.review.llm import LLMRequestError
from app.review.model_config import load_model_config
from app.review.reviewers.schemas import ReviewerResult
from app.review.reviewers.spec_compliance import review_specification
from app.services.llm_usage import QuotaExceededError

PRIVATE = "PRIVATE_INVALID_MODEL_OUTPUT_NEVER_ECHO"
REQUIREMENT = "正文结论应明确说明数据来源。"
BODY = "本模块依据公开数据库得出结论，数据来源见附录。"
HANDOFF = "本模块结论已说明；数据来源为公开数据库；下游需要核对变量口径。"


def spec(number="AI-1", **overrides):
    return {
        "number": number,
        "category": "writing",
        "requirement": REQUIREMENT,
        "check_method": "ai",
        "positive_example": "正例仅用于写法示范，不能当成交付证据。",
        "negative_example": "反例不是真实交付内容。",
        "template_resource_id": None,
        **overrides,
    }


def document(*, location=None, text=BODY, warnings=()):
    return ExtractedDocument(
        format="docx",
        sha256=hashlib.sha256(b"fixed-formal-document").hexdigest(),
        segments=(
            TextSegment(text, location or TextLocation(part="word/document.xml", paragraph=2)),
        ),
        warnings=tuple(warnings),
    )


def finding(number="AI-1", *, excerpt=BODY, segment_id="S1", **overrides):
    return {
        "check_key": number,
        "severity": "blocking",
        "segment_id": segment_id,
        "problem": "该处数据来源说明需要按规范核对。",
        "excerpt": excerpt,
        "suggestion": "核对并补充规范要求的数据来源。",
        **overrides,
    }


def response(*findings, notes=()):
    return {"findings": list(findings), "coverage_notes": list(notes)}


class FakeClient:
    def __init__(self, *responses):
        self.configuration = load_model_config()
        self.responses = list(responses)
        self.requests = []

    def complete_json(self, **request):
        self.requests.append(deepcopy(request))
        if not self.responses:
            raise AssertionError("模型调用超过本轮测试约定")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def review(client, **overrides):
    return review_specification(
        client,
        **{
            "project_id": uuid4(),
            "job_id": uuid4(),
            "items": [spec()],
            "document": document(),
            "handoff_note": HANDOFF,
            "plan": {"argument": "真实方案中的主线。"},
            "history": [],
            **overrides,
        },
    )


def incomplete(result):
    assert result["status"] == "incomplete"
    if result["review"] is not None:
        assert result["review"]["status"] == "incomplete"
        assert result["review"]["findings"] == []
        assert result["review"]["coverage"] is None
    assert PRIVATE not in json.dumps(result, ensure_ascii=False)


def user_payloads(client, call=0):
    payloads = []
    for message in client.requests[call]["messages"]:
        if message["role"] == "user":
            content = message["content"]
            start = content.find("{")
            if start >= 0:
                payloads.append(json.loads(content[start:]))
    return payloads


def test_platform_maps_current_segment_and_derives_check_with_trusted_source():
    client = FakeClient(response(finding()))
    result = review(client)
    assert result["status"] == "completed"
    assert result["checks"][0]["status"] == "issues"
    recorded = result["review"]["findings"][0]
    assert recorded["basis"]["spec_number"] == "AI-1"
    assert recorded["basis"]["excerpt"] == BODY
    assert recorded["basis"]["excerpt_verified"] is True
    assert recorded["location"]["part"] == "word/document.xml"
    assert recorded["location"]["paragraph"] == 2
    assert recorded["model"] == client.configuration.reviewers["spec_compliance"].model
    assert str(recorded["call_id"]) == str(client.requests[0]["call_id"])
    payload = json.dumps(user_payloads(client), ensure_ascii=False)
    assert '"segment_id": "S1"' in payload
    assert '"paragraph"' not in payload and '"location"' not in payload
    assert '"checks"' not in payload


@pytest.mark.parametrize("category", ["writing", "format", "handoff", "template"])
def test_all_ai_categories_and_program_manual_delegation(category):
    client = FakeClient(response())
    result = review(
        client,
        items=[
            spec(category=category),
            spec("P", check_method="program"),
            spec("M", check_method="manual"),
        ],
    )
    assert result["status"] == "completed"
    assert {entry["number"]: entry["status"] for entry in result["checks"]} == {
        "AI-1": "uncovered" if category == "template" else "checked",
        "P": "delegated",
        "M": "delegated",
    }
    payload = json.dumps(user_payloads(client), ensure_ascii=False)
    assert '"number": "P"' not in payload and '"number": "M"' not in payload


@pytest.mark.parametrize(
    "items", [[], [spec(check_method="program")], [spec(check_method="manual")]]
)
def test_no_ai_items_never_calls_model(items):
    client = FakeClient()
    result = review(client, items=items)
    assert result["status"] == "no_ai_items" and result["review"] is None
    assert client.requests == []


def test_all_five_hundred_items_are_derived_without_model_cross_references():
    result = review(FakeClient(response()), items=[spec(f"AI-{i}") for i in range(500)])
    assert result["status"] == "completed" and len(result["checks"]) == 500
    assert all(check["status"] == "checked" for check in result["checks"])


@pytest.mark.parametrize(
    "changes",
    [
        {"check_key": "UNKNOWN"},
        {"segment_id": "S999"},
        {"segment_id": "invalid"},
        {"severity": "passed"},
        {"severity": True},
        {"problem": ""},
        {"excerpt": None},
        {"suggestion": ""},
        {"location": {"page": 99}},
        {"category": "logic"},
    ],
)
def test_invalid_sibling_is_dropped_without_losing_valid_opinion_or_retry(changes):
    client = FakeClient(response(finding(), finding(**changes)))
    result = review(client)
    assert result["status"] == "completed" and len(client.requests) == 1
    assert len(result["review"]["findings"]) == 1
    assert any(
        "1 条意见因格式无效未采用" in note for note in result["review"]["coverage"]["uncovered"]
    )


@pytest.mark.parametrize("excerpt", ["完全没有出现的引文", "其他规范的要求"])
def test_unmatched_excerpt_is_retained_with_explicit_flag(excerpt):
    result = review(FakeClient(response(finding(excerpt=excerpt))))
    assert result["status"] == "completed"
    assert result["review"]["findings"][0]["basis"]["excerpt_verified"] is False
    assert any("摘录未能核对" in note for note in result["review"]["coverage"]["uncovered"])


def test_normalized_width_spaces_and_punctuation_match_current_text():
    result = review(
        FakeClient(response(finding(excerpt="本模块依据公开数据库得出结论, 数据来源见附录.")))
    )
    assert result["review"]["findings"][0]["basis"]["excerpt_verified"] is True


def test_context_excerpt_and_specification_number_are_platform_bound():
    result = review(FakeClient(response(finding(context_key="AI-1", context_excerpt=REQUIREMENT))))
    basis = result["review"]["findings"][0]["basis"]
    assert basis["context_key"] == "AI-1" and basis["context_excerpt_verified"] is True


def test_unknown_context_is_preserved_but_cannot_become_verified_evidence():
    result = review(FakeClient(response(finding(context_key="HISTORY", context_excerpt=BODY))))
    assert result["review"]["findings"][0]["basis"]["context_excerpt_verified"] is False


def test_handoff_uses_program_assigned_segment_and_location():
    result = review(FakeClient(response(finding(segment_id="S2", excerpt=HANDOFF))))
    assert result["review"]["findings"][0]["location"]["part"] == "handoff_note"
    assert result["review"]["findings"][0]["location"]["paragraph"] is None


@pytest.mark.parametrize("notes", [(), ("没有有效位置，未覆盖。",)])
def test_all_invalid_only_completes_with_coverage_note(notes):
    client = FakeClient(response(finding(segment_id="S999"), notes=notes))
    result = review(client)
    assert result["status"] == ("completed" if notes else "incomplete")
    assert len(client.requests) == 1


def test_empty_opinions_and_notes_are_legal_but_absent_material_is_uncovered():
    result = review(
        FakeClient(response()), document=replace(document(), segments=()), handoff_note=""
    )
    assert result["status"] == "completed" and result["checks"][0]["status"] == "uncovered"


def test_platform_extraction_warnings_survive_without_breaking_bounded_schema():
    warnings = tuple(ExtractionWarning(f"unread_{i}", "该部分未读取。") for i in range(101))
    result = review(FakeClient(response()), document=document(warnings=warnings))
    assert result["extraction_warnings"] == [asdict(warning) for warning in warnings]
    ReviewerResult.model_validate_json(json.dumps(result["review"], ensure_ascii=False))


@pytest.mark.parametrize("code", ["timeout", "connection", "provider_error"])
def test_provider_failure_is_incomplete_without_business_retry(code):
    client = FakeClient(LLMRequestError(code))
    incomplete(review(client))
    assert len(client.requests) == 1


def test_quota_errors_propagate():
    with pytest.raises(QuotaExceededError):
        review(FakeClient(QuotaExceededError("project_limit", None)))


@pytest.mark.parametrize(
    "bad_items",
    [
        None,
        {},
        [spec(), spec()],
        [spec()] * 501,
        [spec(unknown=PRIVATE)],
        [spec(requirement="")],
        [spec(number=" ")],
        [spec(check_method="model")],
    ],
)
def test_invalid_spec_snapshot_is_incomplete_before_any_model_call(bad_items):
    client = FakeClient()
    result = review(client, items=bad_items)
    incomplete(result)
    assert client.requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "not-a-uuid"},
        {"job_id": True},
        {"plan": None},
        {"plan": []},
        {"handoff_note": None},
        {"handoff_note": 1},
        {"document": None},
        {"document": {}},
        {"history": None},
        {"history": [{"untrusted": PRIVATE}]},
    ],
)
def test_invalid_business_input_is_incomplete_before_any_model_call(overrides):
    client = FakeClient()
    incomplete(review(client, **overrides))
    assert client.requests == []
