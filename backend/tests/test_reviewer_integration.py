"""真实PostgreSQL + 内存模型替身：框架意见与用量来源一致，不外发或改审核状态。"""

import copy
import hashlib
import json
import socket
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select
from test_deliveries import ready_team
from test_llm_integration import FAKE_KEY, configuration, model_client
from test_llm_usage import (
    KNOWN_USAGE,
    account_snapshot,
    assert_no_calls_or_admissions,
    budget,
    formal_job,
)

from app.models import DeliveryWorkflow, Event
from app.models.llm_usage import ModelCall, ReviewAdmission
from app.models.review_job import ReviewJob
from app.review.reviewers.executor import ReviewerExecutor
from app.review.reviewers.instructions import load_framework_instruction
from app.services.llm_usage import QuotaExceededError, QuotaLimits

PRIVATE = "private-reviewer-manuscript-sentinel-not-accounting"
PRIVATE_HISTORY = "private-historical-opinion-reason-not-accounting"
PRIVATE_OUTPUT = "private-reviewer-output-sentinel-not-accounting"
CONTEXT = {
    "plan": {"research_question": "固定测试研究问题", "note": PRIVATE},
    "specification": {"items": [{"number": "TEST-SPEC", "requirement": "结论须引用所提供证据"}]},
    "delivery": {
        "segments": [{"text": PRIVATE, "location": {"part": "word/document.xml", "paragraph": 2}}]
    },
}
DRAFT = {
    "category": "logic",
    "severity": "important",
    "location": {"description": "正文第2段", "part": "word/document.xml", "paragraph": 2},
    "problem": PRIVATE_OUTPUT,
    "basis": {"spec_number": "TEST-SPEC", "excerpt": "固定测试中的证据边界"},
    "suggestion": "说明证据与结论之间的关系。",
}
VALID_OUTPUT = {
    "findings": [DRAFT],
    "coverage": {"checked": ["固定测试文字片段"], "uncovered": ["来源全文未提供"]},
}
INVALID_OUTPUT = {
    "findings": [{"problem": PRIVATE_OUTPUT}],
    "coverage": {"checked": [], "uncovered": []},
}


def history():
    return [
        {
            "finding_id": str(uuid4()),
            "opinion": copy.deepcopy(DRAFT),
            "handling": "not_adopted",
            "reason": PRIVATE_HISTORY,
            "actor_id": str(uuid4()),
        }
    ]


def response(output=VALID_OUTPUT, *, usage=None):
    raw = json.dumps(output, ensure_ascii=False, allow_nan=False)
    return {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": raw}}],
        "usage": dict(KNOWN_USAGE) if usage is None else usage,
    }


def execute(client, job, *, context=None, history_items=None):
    return ReviewerExecutor(client).run(
        project_id=job.project_id,
        job_id=job.job_id,
        reviewer="spec_compliance",
        instruction=load_framework_instruction(),
        context=copy.deepcopy(CONTEXT) if context is None else context,
        history=history() if history_items is None else history_items,
    )


def state_snapshot(environment, job):
    with environment.sessions() as db:
        return {
            model.__tablename__: dict(
                db.execute(select(model.__table__).where(key == identity)).mappings().one()
            )
            for model, key, identity in [
                (ReviewJob, ReviewJob.id, job.job_id),
                (DeliveryWorkflow, DeliveryWorkflow.delivery_id, job.delivery_id),
            ]
        }


def calls_for(environment):
    with environment.sessions() as db:
        return list(db.scalars(select(ModelCall).order_by(ModelCall.created_at, ModelCall.id)))


def assert_private_metadata_absent(environment):
    encoded = json.dumps(account_snapshot(environment), ensure_ascii=False, default=str)
    with environment.sessions() as db:
        encoded += json.dumps(
            [dict(row) for row in db.execute(select(Event.__table__)).mappings()],
            ensure_ascii=False,
            default=str,
        )
    for private in [PRIVATE, PRIVATE_HISTORY, PRIVATE_OUTPUT, FAKE_KEY]:
        assert private not in encoded


def assert_source_matches_requests_and_ledger(environment, result, requests):
    value = result.to_dict()
    calls = calls_for(environment)
    assert len(value["attempts"]) == len(requests) == len(calls)
    call_by_id = {str(call.id): call for call in calls}
    for attempt, request in zip(value["attempts"], requests, strict=True):
        call = call_by_id[str(attempt["call_id"])]
        encoded = json.dumps(request["messages"], ensure_ascii=False, allow_nan=False).encode()
        expected_hash = hashlib.sha256(encoded).hexdigest()
        assert attempt["input_hash"] == call.input_hash == expected_hash
        assert call.reviewer == "spec_compliance"
        assert call.instruction_version == load_framework_instruction().version
        assert call.model == "fixed-test-model"
    if value["findings"]:
        last = value["attempts"][-1]
        for finding in value["findings"]:
            assert finding["reviewer"] == "spec_compliance"
            assert finding["instruction_version"] == "2026-11-11.1"
            assert finding["model"] == "fixed-test-model"
            assert finding["input_hash"] == last["input_hash"]
            assert str(finding["call_id"]) == str(last["call_id"])
            assert (
                finding["instruction_hash"]
                == hashlib.sha256(requests[-1]["messages"][0]["content"].encode()).hexdigest()
            )
    return value


def test_valid_framework_result_has_exact_sources_and_known_usage_without_workflow_transition(
    auth_environment,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []
    context, history_items = copy.deepcopy(CONTEXT), history()
    unchanged = copy.deepcopy((context, history_items))

    def provider(request):
        payload = json.loads(request.content)
        with auth_environment.sessions() as observer:
            pending = list(observer.scalars(select(ModelCall)))
            assert len(pending) == 1 and pending[0].status == "reserved"
            assert observer.get(ReviewAdmission, pending[0].admission_id).call_count == 1
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        assert PRIVATE in json.dumps(payload["messages"], ensure_ascii=False)
        assert PRIVATE_HISTORY in json.dumps(payload["messages"], ensure_ascii=False)
        requests.append(payload)
        return httpx2.Response(200, json=response())

    client = model_client(auth_environment, transport=httpx2.MockTransport(provider))
    result = execute(client, job, context=context, history_items=history_items)
    value = assert_source_matches_requests_and_ledger(auth_environment, result, requests)
    assert value["status"] == "completed" and len(value["findings"]) == 1
    assert value["findings"][0]["severity"] == "important"
    assert "passed" not in value and "adopted" not in value
    assert (context, history_items) == unchanged
    assert state_snapshot(auth_environment, job) == before
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        assert call.status == "succeeded" and call.total_tokens == call.charged_tokens == 150
        assert budget(db, "platform", call.day).charged_tokens == 150
        assert budget(db, "platform", call.day).reserved_tokens == 0
    assert_private_metadata_absent(auth_environment)


def test_structure_repair_attempts_are_individually_reserved_settled_and_source_bound(
    auth_environment,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        with auth_environment.sessions() as observer:
            records = list(observer.scalars(select(ModelCall)))
            assert len(records) == len(requests)
            assert sum(call.status == "reserved" for call in records) == 1
            if len(requests) == 2:
                assert sum(call.status == "succeeded" for call in records) == 1
                assert sum(call.charged_tokens for call in records) == 150
        return httpx2.Response(
            200, json=response(INVALID_OUTPUT if len(requests) == 1 else VALID_OUTPUT)
        )

    result = execute(model_client(auth_environment, transport=httpx2.MockTransport(provider)), job)
    value = assert_source_matches_requests_and_ledger(auth_environment, result, requests)
    assert value["status"] == "completed" and len(value["attempts"]) == 2
    assert len({str(attempt["call_id"]) for attempt in value["attempts"]}) == 2
    assert len({attempt["input_hash"] for attempt in value["attempts"]}) == 2
    assert requests[0]["messages"] != requests[1]["messages"]
    assert state_snapshot(auth_environment, job) == before
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert all(call.status == "succeeded" and call.charged_tokens == 150 for call in calls)
        assert len({call.admission_id for call in calls}) == 1
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 2
        assert budget(db, "platform", calls[0].day).charged_tokens == 300
        assert budget(db, "platform", calls[0].day).reserved_tokens == 0
        assert budget(db, "platform", calls[0].day).review_count == 1
    assert_private_metadata_absent(auth_environment)


def test_two_invalid_structures_return_incomplete_without_opinions_but_usage_is_charged(
    auth_environment,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response(INVALID_OUTPUT))

    result = execute(model_client(auth_environment, transport=httpx2.MockTransport(provider)), job)
    value = assert_source_matches_requests_and_ledger(auth_environment, result, requests)
    assert value["status"] == "incomplete" and value["findings"] == []
    assert len(value["attempts"]) == 2
    assert PRIVATE_OUTPUT not in json.dumps(value, ensure_ascii=False)
    assert state_snapshot(auth_environment, job) == before
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 2 and sum(call.charged_tokens for call in calls) == 300
        assert budget(db, "platform", calls[0].day).reserved_tokens == 0
    assert_private_metadata_absent(auth_environment)


@pytest.mark.parametrize("failure", ["timeout", "http_error", "response_invalid"])
def test_provider_failure_does_not_start_structure_retry_or_change_queue_state(
    auth_environment, failure
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx2.ReadTimeout(PRIVATE_OUTPUT, request=request)
        body = response()
        if failure == "http_error":
            return httpx2.Response(503, json=body)
        body["choices"] = []
        return httpx2.Response(200, json=body)

    result = execute(model_client(auth_environment, transport=httpx2.MockTransport(provider)), job)
    value = assert_source_matches_requests_and_ledger(auth_environment, result, requests)
    assert value["status"] == "incomplete" and value["findings"] == []
    assert len(value["attempts"]) == 1
    assert PRIVATE_OUTPUT not in json.dumps(value, ensure_ascii=False)
    assert state_snapshot(auth_environment, job) == before
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        if failure == "timeout":
            assert call.status == "unknown" and call.total_tokens is None
            assert budget(db, "platform", call.day).reserved_tokens == call.reserved_tokens > 0
        else:
            assert call.status == "failed" and call.total_tokens == call.charged_tokens == 150
            assert budget(db, "platform", call.day).reserved_tokens == 0
    assert_private_metadata_absent(auth_environment)


def test_second_structure_attempt_quota_failure_propagates_before_second_provider_request(
    auth_environment,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []
    history_items = history()
    preview_messages = []

    class PreviewClient:
        configuration = configuration()

        def complete_json(self, **arguments):
            preview_messages.extend(arguments["messages"])
            return VALID_OUTPUT

    # 先通过公开run构造同一输入，测量实际LLM预留的UTF-8消息字节；不写账、不外发。
    execute(PreviewClient(), job, history_items=history_items)
    encoded = json.dumps(preview_messages, ensure_ascii=False, allow_nan=False).encode()
    first_reservation = len(encoded) + 1024 + 128
    limits = QuotaLimits(platform_daily_tokens=first_reservation + 100)

    def provider(request):
        requests.append(json.loads(request.content))
        with auth_environment.sessions() as db:
            call = db.scalar(select(ModelCall))
            assert call is not None and call.status == "reserved"
            assert call.reserved_tokens == first_reservation
        return httpx2.Response(200, json=response(INVALID_OUTPUT))

    client = model_client(auth_environment, transport=httpx2.MockTransport(provider), limits=limits)
    with pytest.raises(QuotaExceededError) as error:
        execute(client, job, history_items=history_items)
    assert error.value.scope == "platform_tokens" and len(requests) == 1
    assert state_snapshot(auth_environment, job) == before
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].status == "succeeded"
        assert calls[0].charged_tokens == 150 and calls[0].total_tokens == 150
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 1
        assert budget(db, "platform", calls[0].day).reserved_tokens == 0
    assert_private_metadata_absent(auth_environment)


def test_zero_initial_quota_propagates_without_model_request_or_queue_change(auth_environment):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)
    requests = []

    def forbidden(request):
        requests.append(request)
        pytest.fail("Zero quota must deny before any provider request")

    client = model_client(
        auth_environment,
        transport=httpx2.MockTransport(forbidden),
        limits=QuotaLimits(project_daily_reviews=0),
    )
    with pytest.raises(QuotaExceededError):
        execute(client, job)
    assert requests == []
    assert_no_calls_or_admissions(auth_environment)
    assert state_snapshot(auth_environment, job) == before


def test_live_disabled_framework_stays_offline_and_creates_no_usage(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled reviewer must not create a network connection")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = execute(model_client(auth_environment), job)
    value = result.to_dict()
    assert value["status"] == "incomplete" and value["findings"] == []
    assert_no_calls_or_admissions(auth_environment)
    assert state_snapshot(auth_environment, job) == before


def test_completed_empty_findings_still_reports_coverage_without_adopting_delivery(
    auth_environment,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    before = state_snapshot(auth_environment, job)

    def provider(request):
        return httpx2.Response(
            200,
            json=response(
                {"findings": [], "coverage": {"checked": [], "uncovered": ["输入没有可核验的证据"]}}
            ),
        )

    result = execute(model_client(auth_environment, transport=httpx2.MockTransport(provider)), job)
    value = result.to_dict()
    assert value["status"] == "completed" and value["findings"] == []
    assert value["coverage"]["uncovered"] == ["输入没有可核验的证据"]
    assert state_snapshot(auth_environment, job) == before
    assert_private_metadata_absent(auth_environment)
