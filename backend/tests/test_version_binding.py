"""M3-12：独立 PostgreSQL 上的版本标记、重交责任、并发与不可变历史。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from threading import Event as ThreadEvent
from time import monotonic
from uuid import UUID

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_agent_context import context_team, get_context
from test_agents import register_agent
from test_deliveries import delivery_path, upload_delivery
from test_final_review import add_leader, context, decide, pending
from test_modules import module_publication, publish_modules
from test_review_reports import process
from test_specs import publish_spec, specification

from app.models import (
    Agent,
    DeliveryResubmissionRequest,
    DeliveryVersion,
    DeliveryWorkflow,
    Event,
    FinalReviewDecision,
    Notification,
    Project,
    ProjectMember,
    ReviewJob,
)
from app.models.immutable import SnapshotImmutableError
from app.services import agent_permissions, deliveries, plans, version_binding
from app.services.projects import LeaderRequiredError


def adopted(environment):
    team, delivery = pending(environment)
    assert decide(environment, team, delivery)["outcome"] == "adopted"
    return team, delivery


def publish_updates(team, kind="both"):
    plan = (
        publish_modules(team, team.modules, "方案修订后沿用旧交付")
        if kind in {"plan", "both"}
        else team.plan
    )
    spec = (
        publish_spec(team.leader, team.project["id"], specification("规范修订"))
        if kind in {"spec", "both"}
        else team.spec
    )
    return dict(expected_plan_version_id=plan["id"], expected_spec_version_id=spec["id"])


def request(environment, team, delivery, body, *, user_id=None):
    with environment.sessions() as db:
        result = version_binding.request_resubmission(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            UUID(user_id or team.leader_id),
            body,
        )
        return result.id


def info(environment, delivery, *, leader=True):
    with environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(delivery["id"]))
        return version_binding.binding_info(db, [item], leader=leader)[item.id]


def records(environment, delivery):
    with environment.sessions() as db:
        requests = list(
            db.scalars(
                select(DeliveryResubmissionRequest)
                .where(DeliveryResubmissionRequest.delivery_id == UUID(delivery["id"]))
                .order_by(DeliveryResubmissionRequest.requested_at)
            )
        )
        events = list(
            db.scalars(
                select(Event).where(
                    Event.object_id == delivery["id"],
                    Event.event_type == "delivery_resubmission_requested",
                )
            )
        )
        notifications = list(
            db.scalars(
                select(Notification).where(
                    Notification.kind == "delivery_resubmission_requested",
                    Notification.details["delivery_id"].astext == delivery["id"],
                )
            )
        )
        return requests, events, notifications


def frozen_history(environment, delivery):
    with environment.sessions() as db:
        identity = UUID(delivery["id"])
        return {
            model.__tablename__: dict(
                db.execute(
                    select(model.__table__).where(
                        (model.id if model is DeliveryVersion else model.delivery_id) == identity
                    )
                )
                .mappings()
                .one()
            )
            for model in (DeliveryVersion, DeliveryWorkflow, ReviewJob, FinalReviewDecision)
        }


@pytest.mark.parametrize("kind", ["plan", "spec", "both"])
def test_adopted_old_version_labels_follow_each_publication_without_rewriting_history(
    auth_environment, kind
):
    team, delivery = adopted(auth_environment)
    before = frozen_history(auth_environment, delivery)
    initial = info(auth_environment, delivery)
    assert not initial.based_on_old_version and not initial.can_request_resubmission
    target = publish_updates(team, kind)
    binding = info(auth_environment, delivery)
    assert binding.bound_plan_version == binding.bound_spec_version == 1
    assert binding.plan_outdated == (kind in {"plan", "both"})
    assert binding.spec_outdated == (kind in {"spec", "both"})
    assert str(binding.latest_plan_version_id) == target["expected_plan_version_id"]
    assert str(binding.latest_spec_version_id) == target["expected_spec_version_id"]
    assert binding.latest_plan_version == (2 if binding.plan_outdated else 1)
    assert binding.latest_spec_version == (2 if binding.spec_outdated else 1)
    assert binding.based_on_old_version and binding.can_request_resubmission
    assert not info(auth_environment, delivery, leader=False).can_request_resubmission
    assert binding.resubmission_request is None
    assert frozen_history(auth_environment, delivery) == before
    assert records(auth_environment, delivery) == ([], [], [])
    assert request(auth_environment, team, delivery, target) == UUID(delivery["id"])
    requested = info(auth_environment, delivery)
    assert requested.based_on_old_version and not requested.can_request_resubmission
    assert (
        str(requested.resubmission_request.target_plan_version_id)
        == target["expected_plan_version_id"]
    )
    assert (
        str(requested.resubmission_request.target_spec_version_id)
        == target["expected_spec_version_id"]
    )
    assert frozen_history(auth_environment, delivery) == before


def test_concurrent_project_leaders_create_one_request_notification_and_event(auth_environment):
    team, delivery = adopted(auth_environment)
    add_leader(auth_environment, team, team.other_member_id)
    target = publish_updates(team)
    barrier = Barrier(2)

    def send(identity):
        barrier.wait(timeout=5)
        return request(auth_environment, team, delivery, target, user_id=identity)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(send, (team.leader_id, team.other_member_id)))
    assert results == [UUID(delivery["id"])] * 2
    requests, events, notifications = records(auth_environment, delivery)
    assert len(requests) == len(events) == len(notifications) == 1
    record, recorded_event, notification = requests[0], events[0], notifications[0]
    assert record.requested_by in {UUID(team.leader_id), UUID(team.other_member_id)}
    assert recorded_event.actor_id == str(record.requested_by)
    assert recorded_event.actor_type == "human" and recorded_event.represented_user_id is None
    assert recorded_event.plan_version_id == UUID(delivery["plan_version_id"])
    assert recorded_event.spec_version_id == UUID(delivery["spec_version_id"])
    assert recorded_event.task_id == UUID(delivery["module_id"])
    assert record.event_id == recorded_event.id
    assert recorded_event.details["request_id"] == str(record.id)
    assert notification.user_id == UUID(delivery["submitted_by"])
    assert notification.details["request_id"] == str(record.id)
    assert notification.details["module_id"] == delivery["module_id"]
    assert notification.project_id == UUID(team.project["id"])
    assert "方案第 2 版" in notification.message and "规范第 2 版" in notification.message


def test_new_target_version_pair_creates_new_request_and_preserves_prior_target(auth_environment):
    team, delivery = adopted(auth_environment)
    first = publish_updates(team, "plan")
    request(auth_environment, team, delivery, first)
    request(auth_environment, team, delivery, first)
    original = records(auth_environment, delivery)[0][0]
    original_id, original_target = (
        original.id,
        (
            original.target_plan_version_id,
            original.target_spec_version_id,
        ),
    )
    latest_spec = publish_spec(team.leader, team.project["id"], specification("再次更新规范"))
    second = {**first, "expected_spec_version_id": latest_spec["id"]}
    assert info(auth_environment, delivery).can_request_resubmission
    request(auth_environment, team, delivery, second)
    request(auth_environment, team, delivery, second)
    requests, events, notifications = records(auth_environment, delivery)
    assert len(requests) == len(events) == len(notifications) == 2
    assert requests[0].id == original_id
    assert (
        requests[0].target_plan_version_id,
        requests[0].target_spec_version_id,
    ) == original_target
    assert requests[1].target_spec_version_id == UUID(latest_spec["id"])
    assert requests[1].target_plan_version_id == original_target[0]
    assert info(auth_environment, delivery).resubmission_request.id == requests[1].id
    with pytest.raises(version_binding.ResubmissionConflictError):
        request(auth_environment, team, delivery, first)
    assert [len(rows) for rows in records(auth_environment, delivery)] == [2, 2, 2]


@pytest.mark.parametrize("failure", ["after_event_flush", "notification_insert"])
def test_late_failure_rolls_back_request_event_notification_and_preserves_adoption(
    auth_environment, monkeypatch, failure
):
    team, delivery = adopted(auth_environment)
    target = publish_updates(team)
    before = frozen_history(auth_environment, delivery)

    class LateFailure(RuntimeError):
        pass

    def reject_notification(mapper, connection, item):
        if item.kind == "delivery_resubmission_requested":
            raise LateFailure("fixed notification failure")

    if failure == "after_event_flush":
        actual = version_binding.record_event

        def fail_after_flush(*args, **kwargs):
            actual(*args, **kwargs)
            raise LateFailure("fixed event failure")

        monkeypatch.setattr(version_binding, "record_event", fail_after_flush)
    else:
        event.listen(Notification, "before_insert", reject_notification)
    try:
        with pytest.raises(LateFailure):
            request(auth_environment, team, delivery, target)
    finally:
        if failure == "notification_insert":
            event.remove(Notification, "before_insert", reject_notification)
    assert records(auth_environment, delivery) == ([], [], [])
    assert frozen_history(auth_environment, delivery) == before
    assert info(auth_environment, delivery).can_request_resubmission
    monkeypatch.undo()
    request(auth_environment, team, delivery, target)
    assert [len(rows) for rows in records(auth_environment, delivery)] == [1, 1, 1]


def test_resubmission_requests_are_immutable_through_orm_and_raw_postgres(auth_environment):
    team, delivery = adopted(auth_environment)
    target = publish_updates(team)
    request(auth_environment, team, delivery, target)
    original = records(auth_environment, delivery)[0][0]
    with auth_environment.sessions() as db:
        row = db.get(DeliveryResubmissionRequest, original.id)
        row.requested_by = UUID(team.member_id)
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        with pytest.raises(SnapshotImmutableError):
            db.delete(db.get(DeliveryResubmissionRequest, original.id))
            db.flush()
        db.rollback()
        for statement in (
            update(DeliveryResubmissionRequest)
            .where(DeliveryResubmissionRequest.id == original.id)
            .values(requested_by=UUID(team.member_id)),
            delete(DeliveryResubmissionRequest).where(
                DeliveryResubmissionRequest.id == original.id
            ),
        ):
            with pytest.raises(SnapshotImmutableError):
                db.execute(statement)
            db.rollback()
        table = DeliveryResubmissionRequest.__table__
        for statement in (
            update(table)
            .where(table.c.id == original.id)
            .values(requested_by=UUID(team.member_id)),
            delete(table).where(table.c.id == original.id),
        ):
            with pytest.raises(DBAPIError) as failure:
                db.connection().execute(statement)
            assert failure.value.orig.sqlstate == "55000"
            db.rollback()
        schema = db.get_bind().get_execution_options()["schema_translate_map"][None]
        with pytest.raises(DBAPIError) as failure:
            db.execute(text(f'TRUNCATE TABLE "{schema}".delivery_resubmission_requests'))
        assert failure.value.orig.sqlstate == "55000"
        db.rollback()
    after = records(auth_environment, delivery)[0][0]
    assert after.id == original.id and after.requested_by == original.requested_by
    assert after.event_id == original.event_id and after.requested_at == original.requested_at


def test_new_submission_uses_latest_binding_without_revoking_old_adopted_upstream(
    auth_environment,
):
    team = context_team(auth_environment)
    response = upload_delivery(
        team.other_member, team, b"original adopted upstream", module_id=team.modules[0]["id"]
    )
    assert response.status_code == 201
    old = response.json()
    process(auth_environment, team, old)
    decide(auth_environment, team, old)
    final_context = context(auth_environment, team, old)
    frozen = frozen_history(auth_environment, old)
    initial = get_context(auth_environment, team)
    assert [item["content"]["id"] for item in initial["upstream_deliveries"]] == [old["id"]]
    target = publish_updates(team)
    request(auth_environment, team, old, target)
    fresh_response = upload_delivery(
        team.other_member, team, b"new upstream version", module_id=team.modules[0]["id"]
    )
    assert fresh_response.status_code == 201
    fresh = fresh_response.json()
    assert fresh["version"] == old["version"] + 1 and fresh["id"] != old["id"]
    assert fresh["plan_version_id"] == target["expected_plan_version_id"]
    assert fresh["spec_version_id"] == target["expected_spec_version_id"]
    assert fresh["review_status"] == "ai_reviewing"
    assert frozen_history(auth_environment, old) == frozen
    assert context(auth_environment, team, old) == final_context
    assert team.member.get(old["download_url"]).content == b"original adopted upstream"
    handoffs = get_context(auth_environment, team)["upstream_deliveries"]
    assert [item["content"]["id"] for item in handoffs] == [old["id"]]
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        agent_permissions.authorize(db, agent, "read_delivery", delivery_id=UUID(old["id"]))
        with pytest.raises(agent_permissions.AgentPermissionDeniedError):
            agent_permissions.authorize(db, agent, "read_delivery", delivery_id=UUID(fresh["id"]))
    assert info(auth_environment, old).based_on_old_version
    assert not info(auth_environment, fresh).based_on_old_version
    assert not info(auth_environment, fresh).plan_outdated
    assert not info(auth_environment, fresh).spec_outdated


def test_project_lock_waiter_rechecks_latest_target_after_publication(auth_environment):
    team, delivery = adopted(auth_environment)
    target = publish_updates(team)
    project_id, delivery_id, leader_id = (
        UUID(team.project["id"]),
        UUID(delivery["id"]),
        UUID(team.leader_id),
    )
    started, waiter_pids = ThreadEvent(), []

    def waiting_request():
        with auth_environment.sessions() as db:
            # 预热身份映射及旧最新版本，不能让等锁后复用这些数据完成过期操作。
            assert plans.get_latest_plan(db, project_id).id == UUID(
                target["expected_plan_version_id"]
            )
            db.get(ProjectMember, (project_id, leader_id))
            waiter_pids.append(db.scalar(select(func.pg_backend_pid())))
            started.set()
            try:
                version_binding.request_resubmission(db, project_id, delivery_id, leader_id, target)
            except version_binding.ResubmissionConflictError:
                return "stale"
            return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with auth_environment.sessions() as locking:
            locking.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
            locking_pid = locking.scalar(select(func.pg_backend_pid()))
            waiting = executor.submit(waiting_request)
            try:
                assert started.wait(timeout=5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    blockers = locking.scalar(select(func.pg_blocking_pids(waiter_pids[0])))
                    if locking_pid in blockers:
                        break
                    assert not waiting.done(), "重交要求在项目锁释放前完成了"
                    ThreadEvent().wait(0.01)
                else:
                    raise AssertionError("重交要求没有等待已有项目锁")
                newer = plans.publish_plan(
                    locking,
                    project_id,
                    leader_id,
                    module_publication(team.modules, "持锁期间再发布一版"),
                )
                assert str(newer.id) != target["expected_plan_version_id"]
            finally:
                locking.rollback()
        assert waiting.result(timeout=10) == "stale"
    assert records(auth_environment, delivery) == ([], [], [])
    assert info(auth_environment, delivery).latest_plan_version == 3


def test_cached_leader_role_cannot_authorize_after_database_role_changes(auth_environment):
    team, delivery = adopted(auth_environment)
    target = publish_updates(team)
    project_id, leader_id = UUID(team.project["id"]), UUID(team.leader_id)
    with auth_environment.sessions() as stale:
        cached = stale.get(ProjectMember, (project_id, leader_id))
        assert cached.role == "leader"
        with auth_environment.sessions() as changing:
            changing.get(ProjectMember, (project_id, leader_id)).role = "member"
            changing.commit()
        assert cached.role == "leader"
        with pytest.raises(LeaderRequiredError):
            version_binding.request_resubmission(
                stale, project_id, UUID(delivery["id"]), leader_id, target
            )
    assert records(auth_environment, delivery) == ([], [], [])


def test_removed_module_keeps_original_binding_and_request_without_granting_submission_rights(
    auth_environment,
):
    team, delivery = adopted(auth_environment)
    original = frozen_history(auth_environment, delivery)
    created = register_agent(team.member, team.project["id"])
    own_agent_id = UUID(created["agent"]["id"])
    revised = deepcopy(team.modules[1:])
    revised[0]["upstream"] = []
    latest_plan = publish_modules(team, revised, "移除已采用交付所属模块")
    target = {
        "expected_plan_version_id": latest_plan["id"],
        "expected_spec_version_id": team.spec["id"],
    }
    request(auth_environment, team, delivery, target)
    with auth_environment.sessions() as db:
        historical = deliveries.list_deliveries(
            db,
            UUID(team.project["id"]),
            UUID(team.member_id),
            module_id=UUID(delivery["module_id"]),
        )
        assert [str(item.id) for item in historical] == [delivery["id"]]
        assert version_binding.binding_info(db, historical)[historical[0].id].plan_outdated
        agent_permissions.authorize(
            db, db.get(Agent, own_agent_id), "read_delivery", delivery_id=UUID(delivery["id"])
        )
    assert upload_delivery(team.member, team, b"removed module new content").status_code == 404
    assert team.member.get(delivery_path(team)).status_code == 200
    assert team.member.get(delivery["download_url"]).content == b"proof"
    assert frozen_history(auth_environment, delivery) == original
    requests, _, notifications = records(auth_environment, delivery)
    assert len(requests) == len(notifications) == 1
    assert notifications[0].user_id == UUID(delivery["submitted_by"])
    assert info(auth_environment, delivery).resubmission_request.id == requests[0].id
