"""M3-01: state graph, PostgreSQL transactions and immutable review history."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from itertools import product
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_agent_context import context_team, get_context, submit
from test_agents import bearer
from test_deliveries import delivery_path, ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_modules import publish_modules
from test_projects import create_project
from test_specs import publish_spec, specification

from app.models import (
    AgentDraft,
    DeliveryStateTransition,
    DeliveryVersion,
    DeliveryWorkflow,
    DraftConfirmation,
    Event,
)
from app.models.immutable import SnapshotImmutableError
from app.schemas.review_state import ReviewState
from app.services import delivery_states, draft_confirmation
from app.services.delivery_states import (
    InvalidReviewTransitionError,
    ReviewDeliveryNotFoundError,
    ReviewStateConflictError,
    start_review,
    transition_review,
    validate_transition,
)

# Written independently from the service graph: seven states, seven legal edges.
STATES = (
    "pending_confirmation",
    "ai_reviewing",
    "ai_review_partial_failure",
    "returned",
    "pending_final_review",
    "adopted",
    "final_returned",
)
LEGAL_EDGES = {
    ("pending_confirmation", "ai_reviewing"),
    ("ai_reviewing", "ai_review_partial_failure"),
    ("ai_reviewing", "returned"),
    ("ai_reviewing", "pending_final_review"),
    ("ai_review_partial_failure", "ai_reviewing"),
    ("pending_final_review", "adopted"),
    ("pending_final_review", "final_returned"),
}
REVIEW_EVENTS = ("delivery_review_started", "delivery_review_state_changed")


def submitted(team, content=b"original research bytes"):
    response = upload_delivery(team.member, team, content)
    assert response.status_code == 201, response.text
    return response.json()


def persisted_snapshot(db, delivery_id):
    table = DeliveryVersion.__table__
    return dict(db.execute(select(table).where(table.c.id == UUID(delivery_id))).mappings().one())


def ledger(db, delivery_id):
    identity = UUID(delivery_id)
    workflow = db.get(DeliveryWorkflow, identity)
    history = list(
        db.scalars(
            select(DeliveryStateTransition)
            .where(DeliveryStateTransition.delivery_id == identity)
            .order_by(DeliveryStateTransition.revision)
        )
    )
    events = list(
        db.scalars(
            select(Event)
            .where(Event.object_id == delivery_id, Event.event_type.in_(REVIEW_EVENTS))
            .order_by(Event.id)
        )
    )
    return workflow, history, events


def change(environment, team, delivery, target, *, state="ai_reviewing", revision=1):
    with environment.sessions() as db:
        workflow = transition_review(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            target,
            expected_state=state,
            expected_revision=revision,
            actor_id=UUID(team.leader_id),
        )
        db.commit()
        return workflow.state, workflow.revision


def assert_history_links(team, delivery, history, events):
    assert len(history) == len(events)
    assert [item.revision for item in history] == list(range(1, len(history) + 1))
    by_id = {event.id: event for event in events}
    assert len({item.event_id for item in history}) == len(history)
    for item in history:
        event = by_id[item.event_id]
        assert str(event.project_id) == team.project["id"]
        assert event.object_type == "delivery_version"
        assert event.object_id == delivery["id"]
        assert str(event.task_id) == delivery["module_id"]
        assert str(event.plan_version_id) == delivery["plan_version_id"]
        assert str(event.spec_version_id) == delivery["spec_version_id"]
        assert event.details == {
            "from_state": item.from_state,
            "to_state": item.to_state,
            "revision": item.revision,
        }


@pytest.mark.parametrize(("source", "target"), tuple(product(STATES, repeat=2)))
def test_complete_review_transition_matrix_uses_independent_oracle(source, target):
    assert {state.value for state in ReviewState} == set(STATES)
    if (source, target) in LEGAL_EDGES:
        result = validate_transition(source, target)
        assert isinstance(result, ReviewState) and result.value == target
        assert validate_transition(ReviewState(source), ReviewState(target)) is result
    else:
        with pytest.raises(InvalidReviewTransitionError):
            validate_transition(source, target)
        with pytest.raises(InvalidReviewTransitionError):
            validate_transition(ReviewState(source), ReviewState(target))


@pytest.mark.parametrize("unknown", [None, "", "submitted", "review_failed", 1, {}, []])
@pytest.mark.parametrize("unknown_side", ["source", "target"])
def test_unknown_or_malformed_states_are_rejected(unknown, unknown_side):
    with pytest.raises(InvalidReviewTransitionError):
        validate_transition(
            unknown if unknown_side == "source" else "ai_reviewing",
            unknown if unknown_side == "target" else "returned",
        )


def test_formal_submission_initializes_one_review_with_exact_human_and_version_event(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    assert delivery["status"] == "submitted"
    assert delivery["review_status"] == "ai_reviewing"
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert [(item.from_state, item.to_state) for item in history] == [(None, "ai_reviewing")]
        assert_history_links(team, delivery, history, events)
        assert events[0].event_type == "delivery_review_started"
        assert events[0].actor_type == "human"
        assert events[0].actor_id == team.member_id
        assert events[0].represented_user_id is None
        with pytest.raises(ReviewStateConflictError):
            start_review(db, UUID(team.project["id"]), UUID(delivery["id"]))
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert workflow.revision == len(history) == len(events) == 1


def test_private_draft_and_material_have_no_workflow_until_owner_confirms(auth_environment):
    team = draft_team(auth_environment)
    response = upload_material(team.member, team.project["id"], b"supporting material")
    assert response.status_code == 201
    material = response.json()
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(DeliveryWorkflow))) == []
        assert list(db.scalars(select(DeliveryStateTransition))) == []
        for identity in (team.draft["id"], material["id"]):
            with pytest.raises(ReviewDeliveryNotFoundError):
                start_review(db, UUID(team.project["id"]), UUID(identity))
    response = confirm(team)
    assert response.status_code == 201, response.text
    delivery = response.json()
    assert delivery["status"] == "submitted" and delivery["review_status"] == "ai_reviewing"
    assert delivery["sha256"] == team.draft["sha256"]
    with auth_environment.sessions() as db:
        assert [str(item.delivery_id) for item in db.scalars(select(DeliveryWorkflow))] == [
            delivery["id"]
        ]
        workflow, history, events = ledger(db, delivery["id"])
        assert workflow.revision == 1
        assert_history_links(team, delivery, history, events)
        assert events[0].actor_type == "human" and events[0].actor_id == team.member_id
        assert events[0].represented_user_id is None
        assert db.get(AgentDraft, UUID(team.draft["id"])).status == "pending_confirmation"
    assert team.member.get(team.detail).json()["status"] == "confirmed"


def test_review_start_event_failure_rolls_back_entire_formal_submission(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    original_event = delivery_states.record_event

    def fail_after_start_event(db, **values):
        event = original_event(db, **values)
        if values["event_type"] == "delivery_review_started":
            assert db.scalar(select(DeliveryVersion)) is not None
            assert db.scalar(select(DeliveryWorkflow)) is not None
            assert event.id is not None
            raise RuntimeError("review-start event failure")
        return event

    with monkeypatch.context() as patch:
        patch.setattr(delivery_states, "record_event", fail_after_start_event)
        with pytest.raises(RuntimeError, match="review-start event failure"):
            upload_delivery(team.member, team)
    with auth_environment.sessions() as db:
        for model in (DeliveryVersion, DeliveryWorkflow, DeliveryStateTransition):
            assert list(db.scalars(select(model))) == []
        assert (
            list(
                db.scalars(
                    select(Event).where(
                        Event.event_type.in_(["delivery_submitted", *REVIEW_EVENTS])
                    )
                )
            )
            == []
        )
    delivery = submitted(team)
    assert delivery["version"] == 1
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert workflow.revision == len(history) == len(events) == 1


def test_confirmation_failure_after_review_history_flush_rolls_back_and_retries_cleanly(
    auth_environment, monkeypatch
):
    team = draft_team(auth_environment)
    original_event = draft_confirmation.record_event

    def fail_after_confirmation_event(db, **values):
        event = original_event(db, **values)
        if values["event_type"] == "agent_draft_confirmed":
            assert db.scalar(select(DeliveryStateTransition)) is not None
            assert db.scalar(select(DeliveryWorkflow)).revision == 1
            assert db.scalar(select(DraftConfirmation)) is not None
            raise RuntimeError("confirmation after review history failure")
        return event

    with monkeypatch.context() as patch:
        patch.setattr(draft_confirmation, "record_event", fail_after_confirmation_event)
        with pytest.raises(RuntimeError, match="confirmation after review history failure"):
            confirm(team)
    with auth_environment.sessions() as db:
        for model in (
            DeliveryVersion,
            DeliveryWorkflow,
            DeliveryStateTransition,
            DraftConfirmation,
        ):
            assert list(db.scalars(select(model))) == []
        assert (
            list(
                db.scalars(
                    select(Event).where(
                        Event.event_type.in_(
                            ["delivery_submitted", "agent_draft_confirmed", *REVIEW_EVENTS]
                        )
                    )
                )
            )
            == []
        )
    assert team.member.get(team.detail).json()["status"] == "pending_confirmation"
    response = confirm(team)
    assert response.status_code == 201
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, response.json()["id"])
        assert workflow.revision == len(history) == len(events) == 1


def test_transition_is_uncommitted_until_caller_commits_and_outer_rollback_is_atomic(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    with auth_environment.sessions() as db:
        snapshot = persisted_snapshot(db, delivery["id"])
        workflow = transition_review(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            "pending_final_review",
            expected_state="ai_reviewing",
            expected_revision=1,
            actor_id=UUID(team.leader_id),
        )
        assert (workflow.state, workflow.revision) == ("pending_final_review", 2)
        assert persisted_snapshot(db, delivery["id"]) == snapshot
        with auth_environment.sessions() as observer:
            original, history, events = ledger(observer, delivery["id"])
            assert (original.state, original.revision) == ("ai_reviewing", 1)
            assert len(history) == len(events) == 1
        db.rollback()
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert len(history) == len(events) == 1
        assert persisted_snapshot(db, delivery["id"]) == snapshot
    assert change(auth_environment, team, delivery, "pending_final_review") == (
        "pending_final_review",
        2,
    )


def test_transition_event_failure_restores_state_revision_history_event_and_snapshot(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    with auth_environment.sessions() as db:
        snapshot = persisted_snapshot(db, delivery["id"])
    original_event = delivery_states.record_event

    def fail_after_event(db, **values):
        event = original_event(db, **values)
        if values["event_type"] == "delivery_review_state_changed":
            assert db.get(DeliveryWorkflow, UUID(delivery["id"])).revision == 2
            assert event.id is not None
            raise RuntimeError("review transition event failure")
        return event

    with monkeypatch.context() as patch:
        patch.setattr(delivery_states, "record_event", fail_after_event)
        with pytest.raises(RuntimeError, match="review transition event failure"):
            change(auth_environment, team, delivery, "returned")
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert len(history) == len(events) == 1
        assert persisted_snapshot(db, delivery["id"]) == snapshot
    assert change(auth_environment, team, delivery, "returned") == ("returned", 2)


def test_two_postgresql_sessions_with_same_expected_revision_commit_exactly_one_transition(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    with auth_environment.sessions() as db:
        snapshot = persisted_snapshot(db, delivery["id"])
    barrier = Barrier(2)

    def competing_transition(target):
        with auth_environment.sessions() as db:
            # Both ORM identity maps deliberately hold the pre-race value.
            original = db.get(DeliveryWorkflow, UUID(delivery["id"]))
            assert (original.state, original.revision) == ("ai_reviewing", 1)
            barrier.wait(timeout=10)
            try:
                result = transition_review(
                    db,
                    UUID(team.project["id"]),
                    UUID(delivery["id"]),
                    target,
                    expected_state="ai_reviewing",
                    expected_revision=1,
                    actor_id=UUID(team.leader_id),
                )
                db.commit()
                return ("committed", result.state)
            except ReviewStateConflictError:
                return ("conflict", target)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(competing_transition, target)
            for target in ("returned", "pending_final_review")
        ]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(outcome[0] for outcome in outcomes) == ["committed", "conflict"]
    winner = next(target for result, target in outcomes if result == "committed")
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == (winner, 2)
        assert len(history) == len(events) == 2
        assert history[-1].from_state == "ai_reviewing" and history[-1].to_state == winner
        assert events[-1].actor_id == team.leader_id
        assert_history_links(team, delivery, history, events)
        assert persisted_snapshot(db, delivery["id"]) == snapshot


def test_partial_failure_retry_rejects_aba_stale_revision_without_side_effects(auth_environment):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    assert change(auth_environment, team, delivery, "ai_review_partial_failure") == (
        "ai_review_partial_failure",
        2,
    )
    assert change(
        auth_environment,
        team,
        delivery,
        "ai_reviewing",
        state="ai_review_partial_failure",
        revision=2,
    ) == ("ai_reviewing", 3)
    for stale_state, stale_revision, target in (
        ("ai_reviewing", 1, "returned"),
        ("ai_review_partial_failure", 2, "ai_reviewing"),
    ):
        with pytest.raises(ReviewStateConflictError):
            change(
                auth_environment,
                team,
                delivery,
                target,
                state=stale_state,
                revision=stale_revision,
            )
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 3)
        assert len(history) == len(events) == 3
        assert_history_links(team, delivery, history, events)
    assert change(auth_environment, team, delivery, "returned", revision=3) == ("returned", 4)


def test_wrong_project_missing_delivery_and_invalid_transition_leave_review_unchanged(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    foreign = create_project(team.member)
    with auth_environment.sessions() as db:
        for project_id, delivery_id in (
            (UUID(foreign["id"]), UUID(delivery["id"])),
            (UUID(team.project["id"]), uuid4()),
            (uuid4(), UUID(delivery["id"])),
        ):
            with pytest.raises(ReviewDeliveryNotFoundError):
                transition_review(
                    db,
                    project_id,
                    delivery_id,
                    "returned",
                    expected_state="ai_reviewing",
                    expected_revision=1,
                    actor_id=UUID(team.leader_id),
                )
        with pytest.raises(InvalidReviewTransitionError):
            transition_review(
                db,
                UUID(team.project["id"]),
                UUID(delivery["id"]),
                "adopted",
                expected_state="ai_reviewing",
                expected_revision=1,
                actor_id=UUID(team.leader_id),
            )
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert len(history) == len(events) == 1


@pytest.mark.parametrize("terminal", ["returned", "adopted", "final_returned"])
def test_terminal_version_remains_fixed_while_resubmission_starts_independent_latest_review(
    auth_environment,
    terminal,
):
    team = ready_team(auth_environment)
    first = submitted(team, b"original terminal version")
    with auth_environment.sessions() as db:
        snapshot = persisted_snapshot(db, first["id"])
    if terminal == "returned":
        change(auth_environment, team, first, terminal)
        revision = 2
    else:
        change(auth_environment, team, first, "pending_final_review")
        change(auth_environment, team, first, terminal, state="pending_final_review", revision=2)
        revision = 3
    with pytest.raises(InvalidReviewTransitionError):
        change(auth_environment, team, first, "ai_reviewing", state=terminal, revision=revision)
    revised = deepcopy(team.modules)
    revised[0]["goal"] = "新版本才采用的新问题"
    latest_plan = publish_modules(team, revised, "终态后发布新方案")
    latest_spec = publish_spec(team.leader, team.project["id"], specification("终态后新规范"))
    second = submitted(team, b"a separate resubmitted version")
    assert second["id"] != first["id"] and second["version"] == 2
    assert second["plan_version_id"] == latest_plan["id"] != first["plan_version_id"]
    assert second["spec_version_id"] == latest_spec["id"] != first["spec_version_id"]
    assert second["status"] == "submitted" and second["review_status"] == "ai_reviewing"
    with auth_environment.sessions() as db:
        original, history, events = ledger(db, first["id"])
        current, fresh_history, fresh_events = ledger(db, second["id"])
        assert (original.state, original.revision) == (terminal, revision)
        assert len(history) == len(events) == revision
        assert (current.state, current.revision) == ("ai_reviewing", 1)
        assert len(fresh_history) == len(fresh_events) == 1
        assert_history_links(team, first, history, events)
        assert_history_links(team, second, fresh_history, fresh_events)
        assert persisted_snapshot(db, first["id"]) == snapshot
    listed = team.member.get(delivery_path(team)).json()
    old_view = next(item for item in listed if item["id"] == first["id"])
    assert {key: value for key, value in old_view.items() if key != "version_binding"} == {
        **{key: value for key, value in first.items() if key != "version_binding"},
        "review_status": terminal,
    }
    assert old_view["version_binding"]["bound_plan_version"] == 1
    assert old_view["version_binding"]["bound_spec_version"] == 1
    assert old_view["version_binding"]["latest_plan_version_id"] == latest_plan["id"]
    assert old_view["version_binding"]["latest_spec_version_id"] == latest_spec["id"]
    assert old_view["version_binding"]["based_on_old_version"] is (terminal == "adopted")
    assert team.member.get(first["download_url"]).content == b"original terminal version"


def test_review_journal_is_immutable_in_orm_and_postgresql_and_constraints_reject_false_states(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submitted(team)
    with auth_environment.sessions() as db:
        journal = db.scalar(select(DeliveryStateTransition))
        identity = journal.id
        journal.to_state = "adopted"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        journal = db.get(DeliveryStateTransition, identity)
        with pytest.raises(SnapshotImmutableError):
            db.delete(journal)
            db.flush()
        db.rollback()
        table = DeliveryStateTransition.__table__
        for statement in (
            update(table).where(table.c.id == identity).values(to_state="returned"),
            delete(table).where(table.c.id == identity),
        ):
            with pytest.raises(DBAPIError) as failure:
                db.connection().execute(statement)
            assert failure.value.orig.sqlstate == "55000"
            db.rollback()
        original = db.get(DeliveryStateTransition, identity)
        with pytest.raises(IntegrityError) as duplicate:
            db.connection().execute(
                insert(table).values(
                    id=uuid4(),
                    delivery_id=original.delivery_id,
                    revision=original.revision,
                    from_state=None,
                    to_state="ai_reviewing",
                    event_id=original.event_id,
                )
            )
        assert duplicate.value.orig.sqlstate == "23505"
        db.rollback()
        original = db.get(DeliveryStateTransition, identity)
        for values in (
            {"revision": 2, "to_state": "pending_confirmation"},
            {"revision": 2, "from_state": "pending_confirmation"},
            {"revision": 0},
        ):
            with pytest.raises(IntegrityError) as invalid:
                db.connection().execute(
                    insert(table).values(
                        id=uuid4(),
                        delivery_id=UUID(delivery["id"]),
                        revision=values.get("revision", 2),
                        from_state=values.get("from_state", "ai_reviewing"),
                        to_state=values.get("to_state", "returned"),
                        event_id=original.event_id,
                    )
                )
            assert invalid.value.orig.sqlstate == "23514"
            db.rollback()
        workflow_table = DeliveryWorkflow.__table__
        for values in ({"state": "pending_confirmation"}, {"state": "unknown"}, {"revision": 0}):
            with pytest.raises(IntegrityError) as invalid:
                db.connection().execute(
                    update(workflow_table)
                    .where(workflow_table.c.delivery_id == UUID(delivery["id"]))
                    .values(**values)
                )
            assert invalid.value.orig.sqlstate == "23514"
            db.rollback()
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, delivery["id"])
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert len(history) == len(events) == 1


def test_legacy_delivery_without_workflow_remains_null_and_is_not_silently_backfilled(
    auth_environment,
):
    team = ready_team(auth_environment)
    source = submitted(team)
    legacy_id = uuid4()
    with auth_environment.sessions() as db:
        legacy = persisted_snapshot(db, source["id"])
        legacy.update(id=legacy_id, version=2)
        # Simulate a pre-M3 immutable row by insertion, never deleting new audit history.
        db.connection().execute(insert(DeliveryVersion.__table__).values(**legacy))
        db.commit()
    views = team.member.get(delivery_path(team)).json()
    view = next(item for item in views if item["id"] == str(legacy_id))
    assert view["status"] == "submitted" and view["review_status"] is None
    assert team.member.get(view["download_url"]).content == b"original research bytes"
    with auth_environment.sessions() as db:
        workflow, history, events = ledger(db, str(legacy_id))
        assert workflow is None and history == events == []
        assert db.get(DeliveryVersion, legacy_id).review_status is None
        with pytest.raises(ReviewStateConflictError):
            transition_review(
                db,
                UUID(team.project["id"]),
                legacy_id,
                "returned",
                expected_state="ai_reviewing",
                expected_revision=1,
                actor_id=UUID(team.leader_id),
            )
    latest = submitted(team, b"new formal version after migration")
    assert latest["version"] == 3 and latest["review_status"] == "ai_reviewing"
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, legacy_id) is None


def test_agent_upstream_visibility_uses_real_adopted_workflow_and_keeps_dependency_boundary(
    auth_environment,
):
    team = context_team(auth_environment)
    client = auth_environment.make_client()
    headers = bearer(team.token)
    upstream = submit(team, 0, handoff_note="真实已采用上游的交接")
    own = submit(team, 1, client=team.member)
    unrelated = submit(team, 3, client=team.leader, handoff_note="独立附录不能随采用而扩大权限")
    upstream_path = f"/api/agent/deliveries/{upstream['id']}"
    assert client.get(upstream_path, headers=headers).status_code == 403
    assert get_context(auth_environment, team)["upstream_deliveries"] == []
    for delivery in (upstream, unrelated):
        change(auth_environment, team, delivery, "pending_final_review")
        change(
            auth_environment, team, delivery, "adopted", state="pending_final_review", revision=2
        )
    response = client.get(upstream_path, headers=headers)
    assert response.status_code == 200
    assert response.json()["content"]["review_status"] == "adopted"
    assert response.json()["content"]["id"] == upstream["id"]
    assert client.get(upstream_path + "/download", headers=headers).content == b"delivery-content"
    assert client.get(upstream_path + "/review", headers=headers).status_code == 403
    assert (
        client.get(f"/api/agent/deliveries/{unrelated['id']}", headers=headers).status_code == 403
    )
    newest_pending = submit(team, 0, handoff_note="上游未采用的新版本不能覆盖已采用交接")
    assert (
        client.get(f"/api/agent/deliveries/{newest_pending['id']}", headers=headers).status_code
        == 403
    )
    listed = client.get("/api/agent/deliveries", headers=headers)
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["content"]} == {upstream["id"], own["id"]}
    context = get_context(auth_environment, team)
    assert [block["content"]["id"] for block in context["upstream_deliveries"]] == [upstream["id"]]
    with auth_environment.sessions() as db:
        _, history, events = ledger(db, upstream["id"])
        assert len(history) == len(events) == 3
        assert_history_links(team, upstream, history, events)
