"""正式交付逻辑与衔接审核：真实PG绑定和只读完整性，模型始终是内存替身。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import delete, event, select
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_llm_integration import FAKE_KEY, configuration, model_client
from test_llm_usage import KNOWN_USAGE, account_snapshot, assert_no_calls_or_admissions, budget
from test_modules import create_team, module, publish_modules
from test_projects import create_project
from test_specs import publish_spec, specification

from app.models import DeliveryVersion, DeliveryWorkflow, Event, PlanVersion, ReviewJob, SpecVersion
from app.models.llm_usage import ModelCall, ReviewAdmission
from app.review.extract_types import ExtractionError, ExtractionLimits
from app.review.llm import LLMClient
from app.review.model_config import ModelConfiguration
from app.review.reviewers.instructions import load_reasoning_instruction
from app.services import reasoning_review
from app.services.delivery_states import start_review, transition_review
from app.services.llm_usage import QuotaExceededError, QuotaLimits
from app.services.reasoning_review import ReasoningReviewError, review_delivery_reasoning
from app.storage import LocalStorage

PRIVATE = "private-reasoning-service-source-sentinel"
HANDOFF = "private-reasoning-service-handoff-sentinel"


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


def review(
    environment,
    team,
    delivery,
    *,
    storage=None,
    client=None,
    history=(),
    limits=None,
):
    return review_delivery_reasoning(
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

    monkeypatch.setattr(reasoning_review, "review_reasoning", core)
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
    assert old_arguments["specification"] == {"items": team.spec["items"]}
    assert old_arguments["upstream"] == []
    assert old_arguments["module_id"] == UUID(team.modules[0]["id"])
    assert result["module_id"] == team.modules[0]["id"]
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
        with pytest.raises(ReasoningReviewError) as denied:
            review_delivery_reasoning(
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

    with pytest.raises(ReasoningReviewError) as denied:
        review_delivery_reasoning(
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
    with pytest.raises(ReasoningReviewError) as error:
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

    with pytest.raises(ReasoningReviewError) as error:
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


@pytest.mark.parametrize("limits", [{"max_text_chars": 2}, {"max_segments": 1}])
def test_extraction_output_limits_propagate_before_any_review_or_database_lookup(
    auth_environment, captured_core, limits
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, b"first\nsecond")
    before = snapshot(auth_environment)
    with pytest.raises(ExtractionError) as error:
        review(auth_environment, team, formal, limits=replace(ExtractionLimits(), **limits))
    assert error.value.code == "limit_exceeded"
    assert captured_core == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def dependent_team(environment):
    team = create_team(environment)
    grandparent = module("更早资料", [team.leader_id])
    upstream = module("直接上游", [team.other_member_id])
    target = module("本次目标", [team.member_id])
    unrelated = module("无关模块", [team.leader_id])
    upstream["upstream"] = [{"module_id": grandparent["id"], "needs": "更早资料"}]
    target["upstream"] = [{"module_id": upstream["id"], "needs": "直接上游交接结论"}]
    team.modules = [grandparent, upstream, target, unrelated]
    team.plan = publish_modules(team, team.modules)
    team.spec = publish_spec(team.leader, team.project["id"])
    return team


def upstream_upload(environment, team, index, note):
    response = upload_delivery(
        team.other_member if index == 1 else team.leader,
        team,
        content=("private-upstream-original-file:" + note).encode(),
        module_id=team.modules[index]["id"],
        handoff_note=note,
    )
    assert response.status_code == 201
    return response.json()


def adopt(environment, team, delivery):
    for source, target, revision in (
        ("ai_reviewing", "pending_final_review", 1),
        ("pending_final_review", "adopted", 2),
    ):
        with environment.sessions() as db:
            transition_review(
                db,
                UUID(team.project["id"]),
                UUID(delivery["id"]),
                target,
                expected_state=source,
                expected_revision=revision,
                actor_id=team.leader_id,
            )
            db.commit()


def expected_upstream(delivery):
    return {
        "project_id": delivery["project_id"],
        "module_id": delivery["module_id"],
        "delivery_id": delivery["id"],
        "version": delivery["version"],
        "plan_version_id": delivery["plan_version_id"],
        "spec_version_id": delivery["spec_version_id"],
        "sha256": delivery["sha256"],
        "state": "adopted",
        "workflow_revision": 3,
        "handoff_note": delivery["handoff_note"],
    }


def test_only_latest_adopted_direct_upstream_of_bound_plan_is_snapshotted_without_file_reads(
    auth_environment, captured_core
):
    team = dependent_team(auth_environment)
    grandparent = upstream_upload(auth_environment, team, 0, "RECURSIVE_UPSTREAM_MUST_STAY_OUT")
    oldest = upstream_upload(auth_environment, team, 1, "OLD_ADOPTED_MUST_STAY_OUT")
    accepted = upstream_upload(auth_environment, team, 1, "CURRENT_ADOPTED_DIRECT_SUMMARY")
    pending = upstream_upload(auth_environment, team, 1, "NEWER_UNADOPTED_MUST_STAY_OUT")
    unrelated = upstream_upload(auth_environment, team, 3, "UNRELATED_ADOPTED_MUST_STAY_OUT")
    for delivery in (grandparent, oldest, accepted, unrelated):
        adopt(auth_environment, team, delivery)
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])
    revised = deepcopy(team.modules)
    revised[2]["upstream"] = [{"module_id": revised[3]["id"], "needs": "LATEST_DEPENDENCY_ONLY"}]
    publish_modules(team, revised, "最新方案不能改旧交付依赖")
    before = snapshot(auth_environment)
    storage = storage_for(auth_environment)
    for delivery in (grandparent, oldest, accepted, pending, unrelated):
        storage.path(delivery["sha256"]).unlink()
    with auth_environment.sessions() as db:
        engine = db.get_bind()
    statements, reads = [], []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, cursor.rowcount))

    class CurrentFileOnly:
        def path(self, digest):
            assert engine.pool.checkedout() == 0
            assert digest == formal["sha256"]
            reads.append(digest)
            return storage.path(digest)

    event.listen(engine, "after_cursor_execute", observe)
    try:
        result = review(auth_environment, team, formal, storage=CurrentFileOnly())
    finally:
        event.remove(engine, "after_cursor_execute", observe)
    assert result["plan_version_id"] == team.plan["id"]
    assert result["module_id"] == team.modules[2]["id"]
    assert captured_core[-1]["upstream"] == [expected_upstream(accepted)]
    assert captured_core[-1]["plan"] == team.plan["content"]
    assert len(statements) == 2 and "delivery_workflows" in statements[1][0]
    assert "DISTINCT ON" in statements[1][0] and statements[1][1] == 1
    assert reads == [formal["sha256"]]
    for forbidden in (grandparent, oldest, pending, unrelated):
        assert forbidden["handoff_note"] not in repr(captured_core[-1])
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


@pytest.mark.parametrize(
    "state", [None, "ai_reviewing", "returned", "pending_final_review", "final_returned"]
)
def test_dependency_without_adopted_workflow_never_enters_upstream_snapshot(
    auth_environment, captured_core, state
):
    team = dependent_team(auth_environment)
    other = upstream_upload(auth_environment, team, 1, "UNADOPTED_PRIVATE_SUMMARY")
    with auth_environment.sessions() as db:
        workflow = db.get(DeliveryWorkflow, UUID(other["id"]))
        if state is None:
            db.execute(
                delete(DeliveryWorkflow).where(DeliveryWorkflow.delivery_id == workflow.delivery_id)
            )
        else:
            workflow.state = state
        db.commit()
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])
    before = snapshot(auth_environment)
    review(auth_environment, team, formal)
    assert captured_core[-1]["upstream"] == []
    assert captured_core[-1]["plan"]["modules"][2]["upstream"] == team.modules[2]["upstream"]
    assert other["handoff_note"] not in repr(captured_core[-1])
    assert snapshot(auth_environment) == before


def test_upstream_snapshot_is_detached_before_storage_and_does_not_follow_later_adoption(
    auth_environment, captured_core
):
    team = dependent_team(auth_environment)
    accepted = upstream_upload(auth_environment, team, 1, "SELECTED_AT_SNAPSHOT")
    newer = upstream_upload(auth_environment, team, 1, "ADOPTED_AFTER_SNAPSHOT")
    adopt(auth_environment, team, accepted)
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])

    class AdoptAfterDatabaseRead:
        def path(self, digest):
            adopt(auth_environment, team, newer)
            return storage_for(auth_environment).path(digest)

    review(auth_environment, team, formal, storage=AdoptAfterDatabaseRead())
    assert captured_core[-1]["upstream"] == [expected_upstream(accepted)]
    review(auth_environment, team, formal)
    assert captured_core[-1]["upstream"] == [expected_upstream(newer)]


@pytest.mark.parametrize(
    "change",
    ["shape", "missing_module", "duplicate_module", "duplicate_dependency", "self", "unknown"],
)
def test_invalid_bound_plan_refuses_before_storage_or_core(auth_environment, captured_core, change):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    invalid = deepcopy(team.plan["content"])
    if change == "shape":
        invalid["modules"][0]["goal"] = []
    elif change == "missing_module":
        invalid["modules"] = []
    elif change == "duplicate_module":
        invalid["modules"].append(deepcopy(invalid["modules"][0]))
    elif change == "duplicate_dependency":
        dependency = {"module_id": team.modules[1]["id"], "needs": "重复依赖"}
        invalid["modules"][0]["upstream"] = [dependency, deepcopy(dependency)]
    else:
        invalid["modules"][0]["upstream"] = [
            {
                "module_id": team.modules[0]["id"] if change == "self" else str(uuid4()),
                "needs": "无效依赖",
            }
        ]
    with auth_environment.sessions() as db:
        bad_plan_id, delivery_id = uuid4(), uuid4()
        db.execute(
            PlanVersion.__table__.insert().values(
                id=bad_plan_id,
                project_id=UUID(team.project["id"]),
                version=99,
                change_note="malformed fixture only",
                content=invalid,
                published_by=UUID(team.leader_id),
            )
        )
        original = dict(
            db.execute(
                select(DeliveryVersion.__table__).where(DeliveryVersion.id == UUID(formal["id"]))
            )
            .mappings()
            .one()
        )
        original.update(id=delivery_id, version=99, plan_version_id=bad_plan_id)
        db.execute(DeliveryVersion.__table__.insert().values(**original))
        start_review(db, UUID(team.project["id"]), delivery_id)
        job_id = db.scalar(select(ReviewJob.id).where(ReviewJob.delivery_id == delivery_id))
        db.commit()
    bad = {**formal, "id": str(delivery_id), "test_job_id": str(job_id)}
    before = snapshot(auth_environment)

    class ForbiddenStorage:
        def path(self, digest):
            pytest.fail("An invalid bound plan must fail before storage")

    with pytest.raises(ReasoningReviewError) as error:
        review(auth_environment, team, bad, storage=ForbiddenStorage())
    assert error.value.code == "source_unavailable" and captured_core == []
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def reasoning_client(environment, *, transport, limits=None):
    profile = configuration().reviewers["spec_compliance"]
    return LLMClient(
        environment.sessions,
        configuration=ModelConfiguration(reviewers={"logic": profile, "linkage": profile}),
        secrets={"TEST_ONLY_MODEL_KEY": FAKE_KEY},
        live_enabled=False,
        transport=transport,
        quota_limits=limits,
    )


def model_response(output):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)},
            }
        ],
        "usage": dict(KNOWN_USAGE),
    }


def request_data(payload, kind):
    for message in payload["messages"]:
        if message["role"] == "user" and "\n" in message["content"]:
            value = json.loads(message["content"].split("\n", 1)[1])
            if value["kind"] == kind:
                return value["data"]
    raise AssertionError("Expected typed data block is missing")


def output_for(payload, reviewer, *, finding=True):
    plan = request_data(payload, "bound_plan")
    sources = {entry["key"]: entry["text"] for entry in plan["context_sources"]}
    output = {"findings": [], "coverage_notes": ["原始数据与外部全文未核验。"]}
    if not finding:
        return output
    if reviewer == "logic":
        key, reference = "goal", "module.goal"
    else:
        key = next(key for key, text in sources.items() if key.startswith("upstream:") and text)
        reference = key
    segment = request_data(payload, "delivery")["segments"][0]
    output["findings"] = [
        {
            "check_key": key,
            "severity": "important" if reviewer == "logic" else "suggestion",
            "segment_id": segment["segment_id"],
            "problem": "当前文字与已给依据存在需要核对的缺口。",
            "excerpt": segment["text"],
            "suggestion": "对照所给依据补充说明。",
            "context_key": reference,
            "context_excerpt": sources[reference],
        }
    ]
    return output


def assert_business_unchanged_and_private_accounting(environment, before, *private_values):
    after = snapshot(environment)
    for table in ("delivery_versions", "delivery_workflows", "review_jobs"):
        assert after[table] == before[table]
    last_event_id = max(event["id"] for event in before["events"])
    encoded = json.dumps(account_snapshot(environment), ensure_ascii=False, default=str)
    encoded += json.dumps(
        [event for event in after["events"] if event["id"] > last_event_id],
        ensure_ascii=False,
        default=str,
    )
    assert all(private not in encoded for private in (PRIVATE, HANDOFF, FAKE_KEY, *private_values))


def test_real_two_reviewer_sources_use_old_binding_and_only_direct_adopted_summary(
    auth_environment,
):
    team = dependent_team(auth_environment)
    accepted = upstream_upload(auth_environment, team, 1, "ADOPTED_BOUND_HANDOFF_ONLY")
    pending = upstream_upload(auth_environment, team, 1, "UNADOPTED_HANDOFF_MUST_STAY_OUT")
    unrelated = upstream_upload(auth_environment, team, 3, "UNRELATED_HANDOFF_MUST_STAY_OUT")
    grandparent = upstream_upload(auth_environment, team, 0, "RECURSIVE_HANDOFF_MUST_STAY_OUT")
    for value in (accepted, unrelated, grandparent):
        adopt(auth_environment, team, value)
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])
    revised = deepcopy(team.modules)
    revised[2]["goal"] = "LATEST_GOAL_MUST_STAY_OUT"
    revised[2]["upstream"] = [
        {"module_id": revised[3]["id"], "needs": "LATEST_DEPENDENCY_MUST_STAY_OUT"}
    ]
    publish_modules(
        team,
        revised,
        "新方案不得覆盖旧交付",
        terms=[{"term": "LATEST_TERM_MUST_STAY_OUT", "definition": "最新术语"}],
    )
    spec = specification("新规范不得覆盖旧交付")
    spec["items"][0]["requirement"] = "LATEST_SPEC_MUST_STAY_OUT"
    publish_spec(team.leader, team.project["id"], spec)
    before = snapshot(auth_environment)
    requests = {}

    def provider(request):
        payload = json.loads(request.content)
        with auth_environment.sessions() as db:
            calls = list(db.scalars(select(ModelCall).where(ModelCall.status == "reserved")))
            assert len(calls) == 1
            call = calls[0]
            assert db.get(ReviewAdmission, call.admission_id).call_count == len(requests) + 1
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        encoded = json.dumps(payload["messages"], ensure_ascii=False)
        assert PRIVATE in encoded and HANDOFF in encoded
        assert team.spec["items"][0]["requirement"] in encoded
        for forbidden in (pending, unrelated, grandparent):
            assert forbidden["handoff_note"] not in encoded
        assert all(
            value not in encoded
            for value in (
                "LATEST_GOAL_MUST_STAY_OUT",
                "LATEST_TERM_MUST_STAY_OUT",
                "LATEST_SPEC_MUST_STAY_OUT",
                "LATEST_DEPENDENCY_MUST_STAY_OUT",
            )
        )
        assert (accepted["handoff_note"] in encoded) == (call.reviewer == "linkage")
        requests[call.reviewer] = payload
        return httpx2.Response(200, json=model_response(output_for(payload, call.reviewer)))

    result = review(
        auth_environment,
        team,
        formal,
        client=reasoning_client(auth_environment, transport=httpx2.MockTransport(provider)),
    )
    assert result["status"] == "completed" and result["error_code"] is None
    assert (
        result["plan_version_id"] == team.plan["id"]
        and result["spec_version_id"] == team.spec["id"]
    )
    assert result["plan_version"] == result["spec_version"] == 1
    assert (
        result["module_id"] == team.modules[2]["id"]
        and result["document_sha256"] == formal["sha256"]
    )
    expected_source = expected_upstream(accepted)
    expected_source.pop("handoff_note")
    expected_source["handoff_sha256"] = hashlib.sha256(
        accepted["handoff_note"].encode()
    ).hexdigest()
    assert result["upstream_sources"] == [expected_source]
    linkage_delivery = request_data(requests["linkage"], "delivery")
    assert linkage_delivery["upstream_snapshots"] == [expected_upstream(accepted)]
    assert "upstream_snapshots" not in request_data(requests["logic"], "delivery")
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 2 and len({call.admission_id for call in calls}) == 1
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 2
        for call in calls:
            stage = result[call.reviewer]
            request = requests[call.reviewer]
            expected_hash = hashlib.sha256(
                json.dumps(request["messages"], ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
            assert stage["status"] == "completed" and len(stage["attempts"]) == 1
            assert str(call.id) == stage["attempts"][0]["call_id"]
            assert call.input_hash == stage["attempts"][0]["input_hash"] == expected_hash
            assert (
                call.instruction_version
                == stage["instruction_version"]
                == load_reasoning_instruction(call.reviewer).version
            )
            assert (
                stage["instruction_hash"]
                == hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest()
            )
            assert stage["model"] == call.model == "fixed-test-model"
            assert call.status == "succeeded" and call.total_tokens == call.charged_tokens == 150
            finding = stage["findings"][0]
            assert finding["call_id"] == str(call.id) and finding["input_hash"] == call.input_hash
            assert finding["instruction_hash"] == stage["instruction_hash"]
        bucket = budget(db, "platform", calls[0].day)
        assert (bucket.review_count, bucket.charged_tokens, bucket.reserved_tokens) == (1, 300, 0)
    assert storage_for(auth_environment).path(formal["sha256"]).read_bytes() == PRIVATE.encode()
    assert "passed" not in result and "adopted" not in result
    assert_business_unchanged_and_private_accounting(
        auth_environment, before, accepted["handoff_note"]
    )


def test_real_missing_adopted_dependency_returns_uncovered_not_assumed_consistent(auth_environment):
    team = dependent_team(auth_environment)
    upstream_upload(auth_environment, team, 1, "PENDING_PRIVATE_HANDOFF_MUST_STAY_OUT")
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])
    before = snapshot(auth_environment)
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        with auth_environment.sessions() as db:
            reviewer = db.scalar(select(ModelCall.reviewer).where(ModelCall.status == "reserved"))
        calls.append(reviewer)
        assert "PENDING_PRIVATE_HANDOFF_MUST_STAY_OUT" not in request.content.decode()
        return httpx2.Response(
            200, json=model_response(output_for(payload, reviewer, finding=False))
        )

    result = review(
        auth_environment,
        team,
        formal,
        client=reasoning_client(auth_environment, transport=httpx2.MockTransport(provider)),
    )
    assert result["status"] == "completed" and result["upstream_sources"] == []
    key = "upstream:" + team.modules[1]["id"]
    assert (
        next(check for check in result["checks"]["linkage"] if check["key"] == key)["status"]
        == "uncovered"
    )
    assert any(key in value for value in result["coverage"]["uncovered"])
    assert calls == ["logic", "linkage"]
    assert_business_unchanged_and_private_accounting(auth_environment, before)


def test_quota_propagates_before_first_model_and_without_business_writes(auth_environment):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    before = snapshot(auth_environment)

    def forbidden(request):
        pytest.fail("Zero quota must deny before transport")

    with pytest.raises(QuotaExceededError):
        review(
            auth_environment,
            team,
            formal,
            client=reasoning_client(
                auth_environment,
                transport=httpx2.MockTransport(forbidden),
                limits=QuotaLimits(project_daily_reviews=0),
            ),
        )
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_second_reviewer_quota_preserves_settled_first_call_and_original_workflow(auth_environment):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    before = snapshot(auth_environment)
    previews = {}

    class PreviewClient:
        configuration = ModelConfiguration(
            reviewers={
                name: configuration().reviewers["spec_compliance"] for name in ("logic", "linkage")
            }
        )

        def complete_json(self, **arguments):
            previews[arguments["reviewer"]] = arguments["messages"]
            return output_for(
                {"messages": arguments["messages"]}, arguments["reviewer"], finding=False
            )

    preview = review(auth_environment, team, formal, client=PreviewClient())
    assert preview["status"] == "completed" and snapshot(auth_environment) == before
    reservations = {
        name: len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode()) + 1024 + 128
        for name, messages in previews.items()
    }
    assert reservations["linkage"] + 150 > reservations["logic"]
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx2.Response(
            200, json=model_response(output_for(payload, "logic", finding=False))
        )

    with pytest.raises(QuotaExceededError) as denied:
        review(
            auth_environment,
            team,
            formal,
            client=reasoning_client(
                auth_environment,
                transport=httpx2.MockTransport(provider),
                limits=QuotaLimits(platform_daily_tokens=reservations["logic"]),
            ),
        )
    assert denied.value.scope == "platform_tokens" and len(requests) == 1
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].reviewer == "logic"
        assert calls[0].status == "succeeded" and calls[0].charged_tokens == 150
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 1
        assert budget(db, "platform", calls[0].day).reserved_tokens == 0
    assert_business_unchanged_and_private_accounting(auth_environment, before)


@pytest.mark.parametrize("binding", ["plan_version_id", "spec_version_id"])
def test_adopted_upstream_with_foreign_bound_source_is_excluded_before_context(
    auth_environment, captured_core, binding
):
    team = dependent_team(auth_environment)
    original = upstream_upload(auth_environment, team, 1, "PENDING_ORIGINAL_NOT_ADOPTED")
    foreign_project = create_project(team.leader, "上游损坏来源隔离测试")
    with auth_environment.sessions() as db:
        foreign_id = uuid4()
        if binding == "plan_version_id":
            model, content = PlanVersion, {"content": team.plan["content"]}
        else:
            model, content = SpecVersion, {"items": team.spec["items"]}
        db.execute(
            model.__table__.insert().values(
                id=foreign_id,
                project_id=UUID(foreign_project["id"]),
                version=1,
                change_note="foreign fixture only",
                published_by=UUID(team.leader_id),
                **content,
            )
        )
        corrupt_id = uuid4()
        values = dict(
            db.execute(
                select(DeliveryVersion.__table__).where(DeliveryVersion.id == UUID(original["id"]))
            )
            .mappings()
            .one()
        )
        values.update(
            id=corrupt_id,
            version=2,
            handoff_note="FOREIGN_BOUND_ADOPTED_SUMMARY_MUST_STAY_OUT",
        )
        values[binding] = foreign_id
        db.execute(DeliveryVersion.__table__.insert().values(**values))
        start_review(db, UUID(team.project["id"]), corrupt_id)
        db.commit()
    adopt(auth_environment, team, {"id": str(corrupt_id)})
    formal = upload(auth_environment, team, module_id=team.modules[2]["id"])
    before = snapshot(auth_environment)
    review(auth_environment, team, formal)
    assert captured_core[-1]["upstream"] == []
    assert "FOREIGN_BOUND_ADOPTED_SUMMARY_MUST_STAY_OUT" not in repr(captured_core[-1])
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)
