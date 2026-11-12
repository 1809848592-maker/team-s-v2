"""项目审核冻结来源、独立队列、报告验证和可追溯派工；模型固定替身。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_deliveries import ready_team, upload_delivery
from test_modules import publish_modules
from test_project_linkage_pipeline import FixedProjectClient
from test_reasoning_review_service import adopt
from test_specs import publish_spec

from app.models import CollaborationRequest, DeliveryWorkflow, Event
from app.models.immutable import SnapshotImmutableError
from app.models.project_linkage import (
    ProjectLinkageRequest,
    ProjectLinkageResult,
    ProjectLinkageReview,
)
from app.review.project_linkage import execute_project_linkage
from app.services import project_linkage as service
from app.storage import LocalStorage


def adopted_team(environment, *, second=True):
    team = ready_team(environment)
    deliveries = []
    for index in range(2 if second else 1):
        client = team.member if index == 0 else team.other_member
        result = upload_delivery(
            client,
            team,
            f"模块{index}样本量为{120 + index * 30}人。".encode(),
            module_id=team.modules[index]["id"],
        )
        assert result.status_code == 201, result.text
        delivery = result.json()
        adopt(environment, team, delivery)
        deliveries.append(delivery)
    return team, deliveries


def start(environment, team):
    with environment.sessions() as db:
        return service.start_review(db, UUID(team.project["id"]), UUID(team.leader_id))


def claim(environment, **kwargs):
    with environment.sessions() as db:
        result = service.claim_next(db, **kwargs)
        db.commit()
        return result


def detail(environment, team, identity):
    with environment.sessions() as db:
        return service.get_review(
            db, UUID(team.project["id"]), UUID(identity), UUID(team.leader_id)
        )


def execute(environment, claimed, *, client=None):
    result = execute_project_linkage(
        environment.sessions,
        claimed,
        client=client if client is not None else FixedProjectClient(),
        storage=LocalStorage(environment.client.app.state.settings.storage_dir),
    )
    with environment.sessions() as db:
        assert service.complete_job(db, claimed, result)
        db.commit()
    return result


def test_trigger_selects_latest_adopted_only_and_freezes_plan_spec_and_files(auth_environment):
    team, deliveries = adopted_team(auth_environment)
    newer = upload_delivery(
        team.member, team, b"newest adopted", module_id=team.modules[0]["id"]
    ).json()
    adopt(auth_environment, team, newer)
    latest_unadopted = upload_delivery(team.member, team, b"not adopted private-content").json()
    info = start(auth_environment, team)
    plan, spec = (
        publish_modules(team, team.modules, "new plan"),
        publish_spec(team.leader, team.project["id"]),
    )
    after = upload_delivery(team.member, team, b"adopted after frozen").json()
    adopt(auth_environment, team, after)
    with auth_environment.sessions() as db:
        review = db.get(ProjectLinkageReview, UUID(info["id"]))
        assert review.frozen_input["modules"][0]["delivery"]["id"] == newer["id"]
        assert review.frozen_input["modules"][1]["delivery"]["id"] == deliveries[1]["id"]
        assert (
            str(review.plan_version_id) != plan["id"] and str(review.spec_version_id) != spec["id"]
        )
    client = FixedProjectClient()
    execute(auth_environment, claim(auth_environment), client=client)
    final = detail(auth_environment, team, info["id"])
    assert final["status"] == "completed" and final["result"]["coverage"] == "completed"
    assert final["result"]["modules"][0]["delivery_id"] == newer["id"]
    assert latest_unadopted["id"] not in str(final["result"])
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, UUID(latest_unadopted["id"])).state == "ai_reviewing"


def test_missing_adopted_sources_are_uncovered_report_not_fake_approval(auth_environment):
    team, _ = adopted_team(auth_environment, second=False)
    info = start(auth_environment, team)
    execute(auth_environment, claim(auth_environment))
    final = detail(auth_environment, team, info["id"])
    assert final["status"] == "completed" and final["result"]["coverage"] == "incomplete"
    assert final["result"]["modules"][1]["state"] == "uncovered"
    assert "不能视为通过" in final["message"]


def test_network_failure_is_partial_failed_and_retry_keeps_frozen_input_and_history(
    auth_environment,
):
    from app.review.llm import LLMRequestError

    team, _ = adopted_team(auth_environment)
    info = start(auth_environment, team)
    first = claim(auth_environment)
    execute(
        auth_environment,
        first,
        client=FixedProjectClient(failure=LLMRequestError("transport_error")),
    )
    failed = detail(auth_environment, team, info["id"])
    assert failed["status"] == "partial_failed" and failed["can_retry"]
    with auth_environment.sessions() as db:
        frozen = deepcopy(db.get(ProjectLinkageReview, first.review_id).frozen_input)
        service.retry_review(db, first.project_id, first.review_id, UUID(team.leader_id))
    second = claim(auth_environment)
    assert second.attempt == 2 and second.claim_token != first.claim_token
    execute(auth_environment, second)
    with auth_environment.sessions() as db:
        assert db.get(ProjectLinkageReview, first.review_id).frozen_input == frozen
        assert len(list(db.scalars(select(ProjectLinkageResult)))) == 2
        assert not service.complete_job(db, first, {"result_id": str(uuid4())})
    assert detail(auth_environment, team, info["id"])["result"]["coverage"] == "completed"


def test_expired_lease_recovers_and_stale_fences_cannot_store_or_complete(auth_environment):
    team = ready_team(auth_environment)
    info = start(auth_environment, team)
    first = claim(auth_environment, timeout_seconds=1, lease_grace_seconds=0)
    with auth_environment.sessions() as db:
        lease = db.get(ProjectLinkageReview, UUID(info["id"])).lease_until
    assert claim(auth_environment, now=lease - timedelta(microseconds=1)) is None
    assert claim(auth_environment, now=lease, retry_delay_seconds=0) is None
    second = claim(auth_environment, now=lease, timeout_seconds=30)
    assert second.attempt == 2
    with auth_environment.sessions() as db:
        assert not service.complete_job(db, first, {}, now=lease)
        assert not service.fail_job(db, first, "timeout", now=lease)
        db.commit()
    with pytest.raises(service.ProjectLinkageConflictError):
        service.store_result(auth_environment.sessions, first, {})
    execute(auth_environment, second)
    assert detail(auth_environment, team, info["id"])["status"] == "completed"


def test_quota_pause_keeps_attempt_and_reset_time_without_fake_report(auth_environment):
    team = ready_team(auth_environment)
    info = start(auth_environment, team)
    first = claim(auth_environment)
    with auth_environment.sessions() as db:
        now = db.scalar(select(func.clock_timestamp()))
        reset = now + timedelta(days=1)
        assert service.fail_job(db, first, "quota_exceeded", retry_at=reset, now=now)
        db.commit()
    paused = detail(auth_environment, team, info["id"])
    assert paused["status"] == "queued" and paused["attempt"] == 0
    assert paused["result"] is None and paused["error_code"] == "quota_exceeded"
    assert claim(auth_environment, now=reset - timedelta(microseconds=1)) is None
    second = claim(auth_environment, now=reset)
    assert second.attempt == 1 and second.claim_token != first.claim_token


def test_concurrent_claims_have_one_winner_and_skip_locked_project(auth_environment):
    team = ready_team(auth_environment)
    start(auth_environment, team)
    barrier = Barrier(2)

    def take():
        barrier.wait()
        return claim(auth_environment)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: take(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_frozen_input_and_saved_report_are_immutable_in_pg(auth_environment):
    team = ready_team(auth_environment)
    info = start(auth_environment, team)
    with auth_environment.sessions() as db:
        with pytest.raises(DBAPIError):
            db.execute(
                update(ProjectLinkageReview)
                .where(ProjectLinkageReview.id == UUID(info["id"]))
                .values(frozen_input={})
            )
        db.rollback()
    execute(auth_environment, claim(auth_environment))
    with auth_environment.sessions() as db:
        result = db.scalar(select(ProjectLinkageResult))
        result.result = {"spoof": True}
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()


@pytest.mark.parametrize("mutation", ["coverage", "module", "delivery", "summary", "comparison"])
def test_result_validation_cannot_upgrade_uncovered_sources_or_spoof_metadata(
    auth_environment, mutation
):
    from app.review.project_linkage import review_project

    team = ready_team(auth_environment)
    info = start(auth_environment, team)
    claimed = claim(auth_environment)
    with auth_environment.sessions() as db:
        item = db.get(ProjectLinkageReview, UUID(info["id"]))
        result = review_project(
            FixedProjectClient(),
            claimed,
            item.frozen_input,
            storage=LocalStorage(auth_environment.client.app.state.settings.storage_dir),
            plan_version_id=item.plan_version_id,
            spec_version_id=item.spec_version_id,
        )
    if mutation == "coverage":
        result["coverage"], result["coverage_notes"] = "completed", []
    elif mutation == "module":
        result["modules"].pop()
    elif mutation == "delivery":
        result["modules"][0]["delivery_id"] = str(uuid4())
    elif mutation == "summary":
        result["modules"][0].update(state="summarized", summary="fake")
    else:
        result["comparison"]["state"] = "completed"
    with pytest.raises((service.ProjectLinkageConflictError, ValueError)):
        service.store_result(auth_environment.sessions, claimed, result)


def test_complete_refuses_another_review_result_or_claim(auth_environment):
    team = ready_team(auth_environment)
    start(auth_environment, team)
    claimed = claim(auth_environment)
    with auth_environment.sessions() as db:
        assert service.complete_job(db, claimed, {"result_id": str(uuid4())})
        db.commit()
        item = db.get(ProjectLinkageReview, claimed.review_id)
        assert item.status == "queued" and item.result is None


def finished_finding(environment, team):
    from test_project_linkage_pipeline import opinion, summary_outputs

    info = start(environment, team)
    client = FixedProjectClient(
        outputs=[*summary_outputs(), {"findings": [opinion()], "coverage_notes": []}]
    )
    execute(environment, claim(environment), client=client)
    return detail(environment, team, info["id"])


def test_conversion_is_idempotent_and_source_is_platform_generated(auth_environment):
    team, _ = adopted_team(auth_environment)
    info = finished_finding(auth_environment, team)
    finding_id = UUID(info["result"]["findings"][0]["id"])
    body = {
        "title": "统一样本口径",
        "content": "请处理并回应",
        "target_module_id": team.modules[0]["id"],
    }
    values = []
    for _ in range(2):
        with auth_environment.sessions() as db:
            values.append(
                service.convert_finding_to_request(
                    db,
                    UUID(team.project["id"]),
                    UUID(info["id"]),
                    finding_id,
                    UUID(team.leader_id),
                    body,
                )
            )
    assert values[0].id == values[1].id
    assert str(finding_id) in values[0].content and "两模块样本口径不同" in values[0].content
    with auth_environment.sessions() as db:
        assert len(list(db.scalars(select(CollaborationRequest)))) == 1
        assert len(list(db.scalars(select(ProjectLinkageRequest)))) == 1
    final = detail(auth_environment, team, info["id"])
    assert final["result"]["findings"][0]["request_id"] == str(values[0].id)


def test_conversion_late_failure_rolls_back_request_mapping_and_notifications(
    auth_environment, monkeypatch
):
    team, _ = adopted_team(auth_environment)
    info = finished_finding(auth_environment, team)
    original = service._event

    def broken(db, item, event_type, **kwargs):
        if event_type == "project_linkage_request_created":
            raise RuntimeError("fixed-final-write-failure")
        return original(db, item, event_type, **kwargs)

    monkeypatch.setattr(service, "_event", broken)
    with auth_environment.sessions() as db:
        with pytest.raises(RuntimeError):
            service.convert_finding_to_request(
                db,
                UUID(team.project["id"]),
                UUID(info["id"]),
                UUID(info["result"]["findings"][0]["id"]),
                UUID(team.leader_id),
                {"title": "派工", "content": "补充", "target_user_id": team.member_id},
            )
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(CollaborationRequest))) == []
        assert list(db.scalars(select(ProjectLinkageRequest))) == []
        assert db.scalar(select(Event).where(Event.event_type == "request_created")) is None


def test_maximum_legal_finding_can_be_converted_with_excerpted_source(auth_environment):
    team, _ = adopted_team(auth_environment)
    info = finished_finding(auth_environment, team)
    with auth_environment.sessions() as db:
        item = db.get(ProjectLinkageReview, UUID(info["id"]))
        result = deepcopy(item.result)
        finding = result["findings"][0]
        for field in ("problem", "basis", "excerpt", "suggestion"):
            finding[field] = "长" * 6000
        item.result = result
        db.commit()
    with auth_environment.sessions() as db:
        result = service.convert_finding_to_request(
            db,
            UUID(team.project["id"]),
            UUID(info["id"]),
            UUID(finding["id"]),
            UUID(team.leader_id),
            {"title": "派工", "content": "请核对原报告", "target_user_id": team.member_id},
        )
    assert len(result.content) < 10000 and "节选，完整内容见原报告" in result.content
