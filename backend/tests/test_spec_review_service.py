"""正式交付规范审核：真实PG绑定和只读完整性，模型始终是内存替身。"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_llm_integration import model_client
from test_llm_usage import KNOWN_USAGE, assert_no_calls_or_admissions
from test_modules import publish_modules
from test_specs import publish_spec, specification

from app.models import DeliveryVersion, DeliveryWorkflow, Event, ReviewJob
from app.models.llm_usage import ModelCall
from app.review.extract_types import ExtractionError, ExtractionLimits
from app.services import spec_review
from app.services.llm_usage import QuotaExceededError, QuotaLimits
from app.services.spec_review import SpecReviewError, review_delivery_spec
from app.storage import LocalStorage

PRIVATE = "private-spec-service-source-sentinel"
HANDOFF = "private-spec-service-handoff-sentinel"


def storage_for(environment):
    return LocalStorage(environment.client.app.state.settings.storage_dir)


def upload(environment, team, content=PRIVATE.encode(), **changes):
    response = upload_delivery(team.member, team, content, handoff_note=HANDOFF, **changes)
    assert response.status_code == 201
    delivery = response.json()
    with environment.sessions() as db:
        job = db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == UUID(delivery["id"])))
        assert job is not None
        delivery["test_job_id"] = str(job.id)
    return delivery


def review(environment, team, delivery, *, storage=None, client=None, history=(), limits=None):
    return review_delivery_spec(
        environment.sessions,
        project_id=UUID(team.project["id"]),
        delivery_id=UUID(delivery["id"]),
        job_id=UUID(delivery["test_job_id"]),
        storage=storage if storage is not None else storage_for(environment),
        client=client if client is not None else model_client(environment),
        history=history,
        limits=limits,
    )


def snapshot(environment):
    with environment.sessions() as db:
        return {
            model.__tablename__: [
                dict(row) for row in db.execute(select(model.__table__)).mappings()
            ]
            for model in (DeliveryVersion, DeliveryWorkflow, ReviewJob, Event)
        }


@pytest.fixture
def captured_core(monkeypatch):
    calls = []

    def core(client, **arguments):
        calls.append(arguments)
        return {"status": "completed", "findings": [], "coverage": {"uncovered": ["仅服务联测"]}}

    monkeypatch.setattr(spec_review, "review_specification", core)
    return calls


def test_bound_plan_spec_delivery_and_history_survive_newer_publications_without_writes(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    old = upload(auth_environment, team, b"old source\n\nold evidence")
    new_spec = specification("latest changes")
    new_spec["items"][0]["requirement"] = "LATEST_ONLY_SPEC_NOT_OLD"
    latest_spec = publish_spec(team.leader, team.project["id"], new_spec)
    latest_plan = publish_modules(team, team.modules, "latest plan note")
    new = upload(auth_environment, team, b"new source")
    before = snapshot(auth_environment)
    history = [{"not": "interpreted by service"}]
    result = review(auth_environment, team, old, history=history)
    old_arguments = captured_core[-1]
    assert result["status"] == "completed"
    assert result["delivery_id"] == old["id"] and result["delivery_version"] == 1
    assert result["job_id"] == old["test_job_id"]
    assert result["plan_version_id"] == team.plan["id"] and result["plan_version"] == 1
    assert result["spec_version_id"] == team.spec["id"] and result["spec_version"] == 1
    assert [s.text for s in old_arguments["document"].segments] == ["old source", "old evidence"]
    assert [s.location.line_start for s in old_arguments["document"].segments] == [1, 3]
    assert old_arguments["document"].sha256 == old["sha256"]
    assert old_arguments["items"] == team.spec["items"]
    assert old_arguments["handoff_note"] == HANDOFF and old_arguments["history"] == history
    assert "LATEST_ONLY_SPEC_NOT_OLD" not in repr(old_arguments)
    fresh = review(auth_environment, team, new)
    assert fresh["spec_version_id"] == latest_spec["id"] and fresh["spec_version"] == 2
    assert fresh["plan_version_id"] == latest_plan["id"] and fresh["plan_version"] == 2
    assert fresh["delivery_version"] == 2
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_wrong_project_job_shared_material_and_unknown_never_open_storage_or_model(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    other = upload(auth_environment, team, b"other")
    shared = upload_material(team.member, team.project["id"]).json()

    class ForbiddenStorage:
        def path(self, digest):
            pytest.fail("Rejected formal scope must not reach storage")

    for project, delivery, job in [
        (uuid4(), UUID(formal["id"]), UUID(formal["test_job_id"])),
        (UUID(team.project["id"]), UUID(formal["id"]), UUID(other["test_job_id"])),
        (UUID(team.project["id"]), UUID(formal["id"]), uuid4()),
        (UUID(team.project["id"]), UUID(shared["id"]), UUID(formal["test_job_id"])),
        (UUID(team.project["id"]), uuid4(), UUID(formal["test_job_id"])),
    ]:
        with pytest.raises(SpecReviewError) as denied:
            review_delivery_spec(
                auth_environment.sessions,
                project_id=project,
                delivery_id=delivery,
                job_id=job,
                storage=ForbiddenStorage(),
                client=model_client(auth_environment),
                history=[],
            )
        assert denied.value.code == "source_unavailable"
    assert captured_core == []
    assert_no_calls_or_admissions(auth_environment)


def test_private_draft_is_not_read_but_exact_confirmed_formal_version_can_be_reviewed(
    auth_environment, captured_core
):
    team = draft_team(auth_environment)

    class ForbiddenStorage:
        def path(self, digest):
            pytest.fail("A private draft is not a formal review source")

    with pytest.raises(SpecReviewError) as denied:
        review_delivery_spec(
            auth_environment.sessions,
            project_id=UUID(team.project["id"]),
            delivery_id=UUID(team.draft["id"]),
            job_id=uuid4(),
            storage=ForbiddenStorage(),
            client=model_client(auth_environment),
            history=[],
        )
    assert denied.value.code == "source_unavailable" and captured_core == []
    formal = confirm(team).json()
    with auth_environment.sessions() as db:
        formal["test_job_id"] = str(
            db.scalar(select(ReviewJob.id).where(ReviewJob.delivery_id == UUID(formal["id"])))
        )
    result = review(auth_environment, team, formal)
    assert result["delivery_id"] == formal["id"]
    assert captured_core[-1]["document"].sha256 == team.draft["sha256"]


@pytest.mark.parametrize("change", ["missing", "same_length_corrupt", "truncated", "over_limit"])
def test_tampered_original_file_is_fixed_failure_before_model_without_state_writes(
    auth_environment, captured_core, change
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    storage = storage_for(auth_environment)
    path = storage.path(formal["sha256"])
    limits = None
    if change == "missing":
        path.unlink()
    elif change == "same_length_corrupt":
        path.write_bytes(b"x" * formal["size"])
    elif change == "truncated":
        path.write_bytes(b"short")
    else:
        path.write_bytes(b"x" * (formal["size"] + 10))
        limits = replace(ExtractionLimits(), max_file_bytes=formal["size"] + 1)
    before = snapshot(auth_environment)
    with pytest.raises(SpecReviewError) as error:
        review(auth_environment, team, formal, storage=storage, limits=limits)
    assert error.value.code == {
        "missing": "source_unavailable",
        "over_limit": "limit_exceeded",
    }.get(change, "source_mismatch")
    assert PRIVATE not in str(error.value) and str(path) not in str(error.value)
    assert captured_core == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_declared_size_limit_refuses_before_storage_and_db_connection_is_released(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    with auth_environment.sessions() as db:
        pool = db.get_bind().pool
    reads = []

    class InspectStorage:
        def path(self, digest) -> Path:
            assert pool.checkedout() == 0
            reads.append(digest)
            return storage_for(auth_environment).path(digest)

    with pytest.raises(SpecReviewError) as error:
        review(
            auth_environment,
            team,
            formal,
            storage=InspectStorage(),
            limits=replace(ExtractionLimits(), max_file_bytes=2),
        )
    assert error.value.code == "limit_exceeded" and not reads
    review(auth_environment, team, formal, storage=InspectStorage())
    assert reads == [formal["sha256"]] and len(captured_core) == 1


@pytest.mark.parametrize(
    "filename,data,code",
    [
        ("source.bin", b"unknown", "unsupported_format"),
        ("source.pdf", b"not PDF", "invalid_document"),
        ("source.md", b"\xff\xfe", "invalid_encoding"),
    ],
)
def test_unsupported_or_malformed_source_never_calls_model(
    auth_environment, captured_core, filename, data, code
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, data, filename=filename)
    before = snapshot(auth_environment)
    with pytest.raises(ExtractionError) as error:
        review(auth_environment, team, formal)
    assert error.value.code == code and captured_core == []
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def model_response():
    value = {
        "findings": [
            {
                "check_key": "W1",
                "severity": "important",
                "segment_id": "S1",
                "problem": "测试术语需要与方案核对。",
                "excerpt": PRIVATE,
                "context_key": "W1",
                "context_excerpt": "关键术语与项目方案保持一致。",
                "suggestion": "逐项核对术语。",
            }
        ],
        "coverage_notes": ["交接说明的事实来源未核验。"],
    }
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
            }
        ],
        "usage": KNOWN_USAGE,
    }


def test_real_core_receives_only_ai_spec_items_handoff_and_old_binding_with_ledger_source(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    revised = specification("latest version stays out of old review")
    revised["items"][0]["requirement"] = "LATEST_ONLY_RULE_NOT_BOUND_TO_THIS_DELIVERY"
    publish_spec(team.leader, team.project["id"], revised)
    before = snapshot(auth_environment)
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        with auth_environment.sessions() as db:
            assert list(db.scalars(select(ModelCall)))[0].status == "reserved"
        encoded = json.dumps(payload["messages"], ensure_ascii=False)
        assert PRIVATE in encoded and HANDOFF in encoded
        assert team.spec["items"][0]["requirement"] in encoded
        assert team.spec["items"][3]["requirement"] in encoded
        assert team.spec["items"][1]["requirement"] not in encoded
        assert team.spec["items"][2]["requirement"] not in encoded
        assert "LATEST_ONLY_RULE_NOT_BOUND_TO_THIS_DELIVERY" not in encoded
        requests.append(payload)
        return httpx2.Response(200, json=model_response())

    result = review(
        auth_environment,
        team,
        formal,
        client=model_client(auth_environment, transport=httpx2.MockTransport(provider)),
        history=[],
    )
    assert result["status"] == "completed" and len(requests) == 1
    assert result["delivery_id"] == formal["id"] and result["spec_version_id"] == team.spec["id"]
    finding = result["review"]["findings"][0]
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        expected_hash = hashlib.sha256(
            json.dumps(requests[0]["messages"], ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        assert call.status == "succeeded" and call.charged_tokens == 150
        assert call.input_hash == finding["input_hash"] == expected_hash
        assert str(call.id) == finding["call_id"]
        assert call.instruction_version == finding["instruction_version"]
        serialized = json.dumps(
            [
                dict(row)
                for row in db.execute(
                    select(Event.__table__).where(
                        Event.id > max(event["id"] for event in before["events"])
                    )
                ).mappings()
            ],
            default=str,
        )
        assert PRIVATE not in serialized and HANDOFF not in serialized
        ledger = json.dumps(
            dict(db.execute(select(ModelCall.__table__)).mappings().one()), default=str
        )
        assert PRIVATE not in ledger and HANDOFF not in ledger
    after = snapshot(auth_environment)
    for table in ["delivery_versions", "delivery_workflows", "review_jobs"]:
        assert after[table] == before[table]


def test_quota_exception_propagates_without_model_request_or_workflow_change(auth_environment):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    before = snapshot(auth_environment)
    requests = []

    def forbidden(request):
        requests.append(request)
        pytest.fail("Zero quota must refuse before model transport")

    client = model_client(
        auth_environment,
        transport=httpx2.MockTransport(forbidden),
        limits=QuotaLimits(project_daily_reviews=0),
    )
    with pytest.raises(QuotaExceededError):
        review(auth_environment, team, formal, client=client, history=[])
    assert requests == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_no_ai_bound_items_return_explicit_no_ai_without_model_or_workflow_writes(auth_environment):
    team = ready_team(auth_environment)
    only_program = specification("program and manual only")
    for item in only_program["items"]:
        if item["check_method"] == "ai":
            item["check_method"] = "manual"
    bound = publish_spec(team.leader, team.project["id"], only_program)
    formal = upload(auth_environment, team)
    before = snapshot(auth_environment)
    result = review(auth_environment, team, formal, history=[])
    assert result["status"] == "no_ai_items" and result["review"] is None
    assert result["spec_version_id"] == bound["id"] and result["spec_version"] == 2
    assert result["document_sha256"] == formal["sha256"]
    assert all(item["status"] == "delegated" for item in result["checks"])
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)
