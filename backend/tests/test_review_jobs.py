"""M3-02 queue contracts against isolated PostgreSQL, without model/network calls."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from io import BytesIO
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from test_deliveries import ready_team, upload_delivery, upload_material
from test_delivery_states import persisted_snapshot
from test_draft_confirmation import confirm, draft_team

from app.models import DeliveryStateTransition, DeliveryVersion, DeliveryWorkflow, DraftConfirmation
from app.models.event import Event
from app.models.review_job import ReviewJob
from app.services import draft_confirmation
from app.services.deliveries import submit_delivery
from app.services.delivery_states import transition_review
from app.services.review_jobs import claim_next, complete_job, fail_job, retry_job
from app.storage import LocalStorage

FIXED_RESULT = {"coverage": "not_covered", "source": "fixed_test_handler", "findings": []}


def queued_delivery(environment):
    team = ready_team(environment)
    response = upload_delivery(team.member, team, b"review queue original bytes")
    assert response.status_code == 201, response.text
    return team, response.json()


def database_time(environment):
    with environment.sessions() as db:
        # Future supplied time makes the just-committed queued job due without sleeping.
        return db.scalar(select(func.clock_timestamp())) + timedelta(seconds=1)


def job_for(db, delivery):
    return db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == UUID(delivery["id"])))


def claim(environment, now, **options):
    with environment.sessions() as db:
        claimed = claim_next(db, now=now, **options)
        db.commit()
        return claimed


def finish(environment, claimed, now, result=None):
    with environment.sessions() as db:
        accepted = complete_job(db, claimed, FIXED_RESULT if result is None else result, now=now)
        db.commit()
        return accepted


def fail(environment, claimed, now, error_code="timeout", **options):
    with environment.sessions() as db:
        accepted = fail_job(db, claimed, error_code, now=now, **options)
        db.commit()
        return accepted


def review_snapshot(environment, team, delivery):
    with environment.sessions() as db:
        result = {}
        for name, table, condition in (
            ("delivery", DeliveryVersion.__table__, DeliveryVersion.id == UUID(delivery["id"])),
            ("job", ReviewJob.__table__, ReviewJob.delivery_id == UUID(delivery["id"])),
            (
                "workflow",
                DeliveryWorkflow.__table__,
                DeliveryWorkflow.delivery_id == UUID(delivery["id"]),
            ),
            (
                "history",
                DeliveryStateTransition.__table__,
                DeliveryStateTransition.delivery_id == UUID(delivery["id"]),
            ),
            ("events", Event.__table__, Event.project_id == UUID(team.project["id"])),
        ):
            result[name] = [
                dict(row) for row in db.execute(select(table).where(condition)).mappings()
            ]
        return result


def assert_no_review_pass(db, delivery):
    workflow = db.get(DeliveryWorkflow, UUID(delivery["id"]))
    assert workflow.state not in {"pending_final_review", "adopted"}
    targets = list(
        db.scalars(
            select(DeliveryStateTransition.to_state).where(
                DeliveryStateTransition.delivery_id == UUID(delivery["id"])
            )
        )
    )
    assert "pending_final_review" not in targets and "adopted" not in targets


def test_formal_submission_and_owner_confirmation_enqueue_one_job_but_draft_and_material_do_not(
    auth_environment,
):
    team = draft_team(auth_environment)
    material = upload_material(team.member, team.project["id"], b"shared evidence")
    assert material.status_code == 201
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ReviewJob))) == []
    response = confirm(team)
    assert response.status_code == 201, response.text
    delivery = response.json()
    with auth_environment.sessions() as db:
        jobs = list(db.scalars(select(ReviewJob)))
        assert len(jobs) == 1
        job = jobs[0]
        assert str(job.delivery_id) == delivery["id"]
        assert (job.status, job.attempt, job.workflow_revision) == ("queued", 0, 1)
        assert job.claim_token is None and job.lease_until is None
        assert job.result is None and job.finished_at is None
        assert db.get(DeliveryWorkflow, job.delivery_id).state == "ai_reviewing"
    assert confirm(team).status_code == 409
    direct = upload_delivery(team.member, team, module_id=team.modules[1]["id"])
    assert direct.status_code == 201
    with auth_environment.sessions() as db:
        jobs = list(db.scalars(select(ReviewJob)))
        assert len(jobs) == 2
        assert {str(job.delivery_id) for job in jobs} == {delivery["id"], direct.json()["id"]}


def test_submission_queue_workflow_and_events_wait_for_caller_commit_and_rollback_together(
    auth_environment,
):
    team = ready_team(auth_environment)
    settings = team.member.app.state.settings
    with auth_environment.sessions() as db:
        delivery = submit_delivery(
            db,
            UUID(team.project["id"]),
            UUID(team.modules[0]["id"]),
            UUID(team.member_id),
            "transaction.md",
            BytesIO(b"transactional queue bytes"),
            {"ai_participation": "none", "disclosure_version": "deepseek-2026-11-12.1"},
            storage=LocalStorage(settings.storage_dir),
            max_bytes=settings.upload_max_bytes,
            commit=False,
        )
        identity = delivery.id
        assert db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == identity)) is not None
        assert db.get(DeliveryWorkflow, identity) is not None
        with auth_environment.sessions() as observer:
            assert observer.get(DeliveryVersion, identity) is None
            assert list(observer.scalars(select(ReviewJob))) == []
            assert list(observer.scalars(select(DeliveryWorkflow))) == []
        db.rollback()
    with auth_environment.sessions() as db:
        assert db.get(DeliveryVersion, identity) is None
        assert list(db.scalars(select(ReviewJob))) == []
        assert list(db.scalars(select(DeliveryStateTransition))) == []
        assert list(db.scalars(select(Event).where(Event.object_id == str(identity)))) == []
    response = upload_delivery(team.member, team)
    assert response.status_code == 201 and response.json()["version"] == 1


def test_late_confirmation_event_failure_leaves_no_orphan_queue_and_retry_starts_once(
    auth_environment,
    monkeypatch,
):
    team = draft_team(auth_environment)
    original = draft_confirmation.record_event

    def fail_after_queued_confirmation(db, **values):
        event = original(db, **values)
        if values["event_type"] == "agent_draft_confirmed":
            assert db.scalar(select(ReviewJob)) is not None
            assert db.scalar(select(DeliveryStateTransition)) is not None
            raise RuntimeError("queue transaction failure")
        return event

    with monkeypatch.context() as patch:
        patch.setattr(draft_confirmation, "record_event", fail_after_queued_confirmation)
        with pytest.raises(RuntimeError, match="queue transaction failure"):
            confirm(team)
    with auth_environment.sessions() as db:
        for model in (
            DeliveryVersion,
            ReviewJob,
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
                            [
                                "delivery_submitted",
                                "delivery_review_started",
                                "agent_draft_confirmed",
                            ]
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
        jobs = list(db.scalars(select(ReviewJob)))
        assert len(jobs) == 1 and jobs[0].attempt == 0
        assert str(jobs[0].delivery_id) == response.json()["id"]


def test_claim_changes_are_uncommitted_and_rollback_keeps_job_claimable(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    before = review_snapshot(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        claimed = claim_next(db, now=now, timeout_seconds=20, lease_grace_seconds=1)
        assert claimed is not None
        assert claimed.delivery_id == UUID(delivery["id"])
        assert claimed.project_id == UUID(team.project["id"])
        assert claimed.attempt == claimed.workflow_revision == 1
        assert review_snapshot(auth_environment, team, delivery) == before
        db.rollback()
    assert review_snapshot(auth_environment, team, delivery) == before
    claimed = claim(auth_environment, now)
    assert claimed is not None and claimed.attempt == 1
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert job.status == "running" and job.claim_token == claimed.claim_token
        assert job.lease_until > now and job.started_at is not None


def test_two_independent_workers_cannot_claim_same_due_job(auth_environment):
    _, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    barrier = Barrier(2)

    def take():
        with auth_environment.sessions() as db:
            # Both sessions deliberately cache the old queued row before contending.
            assert job_for(db, delivery).status == "queued"
            barrier.wait(timeout=10)
            claimed = claim_next(db, now=now)
            db.commit()
            return claimed

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(take), executor.submit(take)]
        outcomes = [future.result(timeout=20) for future in futures]
    claims = [item for item in outcomes if item is not None]
    assert len(claims) == 1
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt) == ("running", 1)
        assert job.claim_token == claims[0].claim_token
        assert db.get(DeliveryWorkflow, job.delivery_id).revision == 1


def test_duplicate_completion_commits_once_and_does_not_approve_delivery(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now)
    assert claimed is not None
    barrier = Barrier(2)

    def commit_result(worker):
        with auth_environment.sessions() as db:
            barrier.wait(timeout=10)
            accepted = complete_job(db, claimed, {**FIXED_RESULT, "worker": worker}, now=now)
            db.commit()
            return worker, accepted

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit_result, worker) for worker in ("A", "B")]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(accepted for _, accepted in outcomes) == [False, True]
    winner = next(worker for worker, accepted in outcomes if accepted)
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert job.status == "succeeded" and job.attempt == 1
        assert job.result == {**FIXED_RESULT, "worker": winner}
        assert job.claim_token is None and job.lease_until is None
        assert job.finished_at is not None
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert_no_review_pass(db, delivery)
    after = review_snapshot(auth_environment, team, delivery)
    assert finish(auth_environment, claimed, now) is False
    assert fail(auth_environment, claimed, now) is False
    assert review_snapshot(auth_environment, team, delivery) == after


def test_completion_is_not_visible_until_caller_commits_and_rollback_allows_retry(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now)
    assert claimed is not None
    before = review_snapshot(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        assert complete_job(db, claimed, FIXED_RESULT, now=now) is True
        assert job_for(db, delivery).status == "succeeded"
        assert review_snapshot(auth_environment, team, delivery) == before
        db.rollback()
    assert review_snapshot(auth_environment, team, delivery) == before
    assert finish(auth_environment, claimed, now) is True


def test_wrong_claim_token_and_workflow_revision_are_fenced_without_side_effects(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now)
    assert claimed is not None
    before = review_snapshot(auth_environment, team, delivery)
    for stale in (replace(claimed, claim_token=uuid4()), replace(claimed, workflow_revision=0)):
        assert finish(auth_environment, stale, now) is False
        assert fail(auth_environment, stale, now) is False
        assert review_snapshot(auth_environment, team, delivery) == before
    with auth_environment.sessions() as db:
        transition_review(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            "returned",
            expected_state="ai_reviewing",
            expected_revision=1,
            actor_id=UUID(team.leader_id),
        )
        db.commit()
    changed = review_snapshot(auth_environment, team, delivery)
    assert finish(auth_environment, claimed, now) is False
    assert fail(auth_environment, claimed, now) is False
    assert review_snapshot(auth_environment, team, delivery) == changed


def test_expired_claim_cannot_finish_or_fail_even_before_another_worker_reclaims(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now, timeout_seconds=2, lease_grace_seconds=1)
    assert claimed is not None
    with auth_environment.sessions() as db:
        expired_at = job_for(db, delivery).lease_until
    before = review_snapshot(auth_environment, team, delivery)
    assert finish(auth_environment, claimed, expired_at) is False
    assert fail(auth_environment, claimed, expired_at) is False
    assert review_snapshot(auth_environment, team, delivery) == before


def test_expired_lease_is_recovered_by_fresh_database_engine_and_old_worker_is_fenced(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    first = claim(auth_environment, now, timeout_seconds=2, lease_grace_seconds=1)
    assert first is not None
    with auth_environment.sessions() as db:
        original = job_for(db, delivery)
        lease_until = original.lease_until
        job_id = original.id
        bind = db.get_bind()
        database_url = bind.url
        schema_options = bind.get_execution_options()
    # Simulate a completely new worker's DB pool; no original ORM object/session is reused.
    engine = create_engine(database_url, pool_pre_ping=True)
    fresh_sessions = sessionmaker(
        bind=engine.execution_options(**schema_options), expire_on_commit=False
    )
    try:
        with fresh_sessions() as db:
            assert claim_next(db, now=lease_until - timedelta(microseconds=1)) is None
            db.commit()
        with fresh_sessions() as db:
            assert claim_next(db, now=lease_until, retry_delay_seconds=7) is None
            db.commit()
        with fresh_sessions() as db:
            recovered = db.get(ReviewJob, job_id)
            assert (recovered.status, recovered.attempt) == ("retry_wait", 1)
            assert recovered.last_error == "lease_expired"
            assert recovered.claim_token is None and recovered.lease_until is None
            assert recovered.available_at >= lease_until + timedelta(seconds=7)
            due_at = recovered.available_at
            workflow = db.get(DeliveryWorkflow, recovered.delivery_id)
            assert (workflow.state, workflow.revision) == ("ai_review_partial_failure", 2)
            assert_no_review_pass(db, delivery)
        assert claim(auth_environment, due_at - timedelta(microseconds=1)) is None
        with fresh_sessions() as db:
            second = claim_next(db, now=due_at)
            db.commit()
        assert second is not None and second.job_id == first.job_id
        assert second.claim_token != first.claim_token and second.attempt == 2
        assert second.workflow_revision == 3
        before = review_snapshot(auth_environment, team, delivery)
        assert finish(auth_environment, first, due_at) is False
        assert fail(auth_environment, first, due_at) is False
        assert review_snapshot(auth_environment, team, delivery) == before
        assert finish(auth_environment, second, due_at) is True
    finally:
        engine.dispose()


def test_retryable_failure_waits_until_due_then_retries_partial_failure_with_new_revision(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    first = claim(auth_environment, now)
    assert first is not None
    assert fail(auth_environment, first, now, retry_delay_seconds=9) is True
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt) == ("retry_wait", 1)
        assert job.last_error == "timeout"
        assert job.result is None and job.claim_token is None and job.lease_until is None
        due_at = job.available_at
        assert due_at >= now + timedelta(seconds=9)
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert (workflow.state, workflow.revision) == ("ai_review_partial_failure", 2)
    assert claim(auth_environment, due_at - timedelta(microseconds=1)) is None
    second = claim(auth_environment, due_at)
    assert second is not None and second.attempt == 2 and second.workflow_revision == 3
    assert second.claim_token != first.claim_token
    before = review_snapshot(auth_environment, team, delivery)
    assert fail(auth_environment, first, due_at) is False
    assert finish(auth_environment, first, due_at) is False
    assert review_snapshot(auth_environment, team, delivery) == before
    assert finish(auth_environment, second, due_at) is True
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "ai_reviewing"
        assert_no_review_pass(db, delivery)


def test_retry_limit_stops_automatic_claims_and_manual_retry_keeps_attempt_history(
    auth_environment,
):
    _, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    for attempt in (1, 2):
        claimed = claim(auth_environment, now, max_attempts=2)
        assert claimed is not None and claimed.attempt == attempt
        assert fail(auth_environment, claimed, now, retry_delay_seconds=0) is True
        with auth_environment.sessions() as db:
            job = job_for(db, delivery)
            now = job.available_at
            assert job.status == ("retry_wait" if attempt == 1 else "failed")
    assert claim(auth_environment, now + timedelta(days=1), max_attempts=2) is None
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        previous_attempt, previous_limit = job.attempt, job.max_attempts
        assert job.finished_at is not None
        identity = job.id
        retried = retry_job(db, UUID(delivery["id"]), now=now)
        assert retried.id == identity and retried.attempt == previous_attempt == 2
        assert retried.max_attempts == previous_limit + 3
        assert retried.status in {"queued", "retry_wait"}
        db.commit()
    third = claim(auth_environment, now, max_attempts=previous_limit + 3)
    assert third is not None and third.attempt == 3
    assert finish(auth_environment, third, now) is True


def test_pipeline_not_ready_is_nonretryable_and_never_promotes_to_passed(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now)
    assert claimed is not None
    assert fail(auth_environment, claimed, now, "pipeline_not_ready", retryable=False) is True
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert job.status == "failed" and job.attempt == 1
        assert job.last_error == "pipeline_not_ready" and job.result is None
        assert job.finished_at is not None
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert workflow.state == "ai_review_partial_failure"
        assert_no_review_pass(db, delivery)
    before = review_snapshot(auth_environment, team, delivery)
    assert claim(auth_environment, now + timedelta(days=1)) is None
    assert review_snapshot(auth_environment, team, delivery) == before


def test_postgresql_has_one_job_per_delivery_and_does_not_enqueue_legacy_snapshots(
    auth_environment,
):
    _, delivery = queued_delivery(auth_environment)
    legacy_id = uuid4()
    with auth_environment.sessions() as db:
        job_table = ReviewJob.__table__
        original = dict(db.execute(select(job_table)).mappings().one())
        duplicate = {**original, "id": uuid4()}
        with pytest.raises(IntegrityError) as error:
            db.connection().execute(insert(job_table).values(**duplicate))
        assert error.value.orig.sqlstate == "23505"
        db.rollback()
        legacy = persisted_snapshot(db, delivery["id"])
        legacy.update(id=legacy_id, version=2)
        db.connection().execute(insert(DeliveryVersion.__table__).values(**legacy))
        db.commit()
    with auth_environment.sessions() as db:
        assert db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == legacy_id)) is None
        assert db.get(DeliveryWorkflow, legacy_id) is None
        assert len(list(db.scalars(select(ReviewJob)))) == 1


def test_invalid_error_and_unbounded_or_non_json_results_are_rejected_without_partial_writes(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now)
    assert claimed is not None
    before = review_snapshot(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        with pytest.raises(ValueError):
            fail_job(db, claimed, "raw_exception_with_private_text", now=now)
        for invalid in (["not an object"], {"value": float("nan")}, {"text": "x" * 65537}):
            with pytest.raises(ValueError):
                complete_job(db, claimed, invalid, now=now)
    assert review_snapshot(auth_environment, team, delivery) == before
    assert finish(auth_environment, claimed, now) is True
