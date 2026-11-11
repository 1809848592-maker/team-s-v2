"""Real spawned workers, bounded execution and PostgreSQL restart recovery."""

import json
import multiprocessing
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields
from datetime import timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_review_jobs import (
    assert_no_review_pass,
    job_for,
    queued_delivery,
    review_snapshot,
)

from app.models import DeliveryStateTransition, DeliveryWorkflow, Event
from app.services.review_jobs import claim_next, complete_job
from app.worker import run_once

PRIVATE_EXCEPTION = "test-private-worker-exception-never-persist-or-print"


def fixed_pid_handler(review):
    """Importable spawn handler: immutable primitive identity, no DB or credentials."""
    names = {field.name for field in fields(review)}
    assert names == {"job_id", "delivery_id", "project_id", "attempt", "workflow_revision"}
    with pytest.raises(FrozenInstanceError):
        review.attempt = 99
    return {
        "coverage": "not_covered",
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "delivery_id": str(review.delivery_id),
        "project_id": str(review.project_id),
        "attempt": review.attempt,
        "workflow_revision": review.workflow_revision,
    }


def marker_sleep_handler(review):
    marker = Path(os.environ["TEAMS_TEST_WORKER_MARKER"])
    marker.write_text(json.dumps({"pid": os.getpid(), "delivery_id": str(review.delivery_id)}))
    # The worker must end this real child; waiting for this function to return would fail the test.
    time.sleep(30)
    return {"coverage": "not_covered", "unexpected_sleep_completion": True}


def private_error_handler(review):
    print(PRIVATE_EXCEPTION, flush=True)
    print(PRIVATE_EXCEPTION, file=sys.stderr, flush=True)
    raise RuntimeError(PRIVATE_EXCEPTION)


def oversized_handler(review):
    return {"text": "x" * (70 * 1024)}


def non_json_handler(review):
    return {"not_json": {"a set"}}


def process_exists(identity):
    try:
        os.kill(identity, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_marker(path, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError):
                pass
        ThreadEvent().wait(0.02)
    pytest.fail("Spawned worker did not enter its fixed handler before the deadline")


def database_binding(environment):
    with environment.sessions() as db:
        bind = db.get_bind()
        return bind.url, dict(bind.get_execution_options())


def claim_then_wait(database_url, engine_options, sender):
    """A real worker persists a claim, then is killed by the parent without cleanup."""
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine.execution_options(**engine_options), expire_on_commit=False)
    try:
        with sessions() as db:
            claimed = claim_next(db, timeout_seconds=30, lease_grace_seconds=1)
            db.commit()
        sender.send((os.getpid(), claimed))
        sender.close()
        time.sleep(30)
    finally:
        sender.close()
        engine.dispose()


def recover_in_new_worker(database_url, engine_options, lease_until, sender):
    """Recovery uses a fresh process/pool and explicit clock boundaries, without sleeps."""
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine.execution_options(**engine_options), expire_on_commit=False)
    try:
        with sessions() as db:
            early = claim_next(db, now=lease_until - timedelta(microseconds=1))
            db.commit()
        with sessions() as db:
            reclaimed_at_expiry = claim_next(db, now=lease_until, retry_delay_seconds=0)
            db.commit()
        with sessions() as db:
            restarted = claim_next(db, now=lease_until)
            db.commit()
        with sessions() as db:
            accepted = complete_job(
                db,
                restarted,
                {"coverage": "not_covered", "restarted_pid": os.getpid()},
                now=lease_until,
            )
            db.commit()
        sender.send(
            {
                "pid": os.getpid(),
                "early_claim": early,
                "expiry_claim": reclaimed_at_expiry,
                "restarted": restarted,
                "accepted": accepted,
            }
        )
    finally:
        sender.close()
        engine.dispose()


def test_default_real_spawn_marks_pipeline_not_ready_without_approving_delivery(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    assert run_once(auth_environment.sessions, timeout_seconds=5, lease_grace_seconds=2) is True
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt, job.last_error) == ("failed", 1, "pipeline_not_ready")
        assert job.result is None and job.claim_token is None and job.lease_until is None
        assert job.finished_at is not None
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert (workflow.state, workflow.revision) == ("ai_review_partial_failure", 2)
        assert_no_review_pass(db, delivery)
    before = review_snapshot(auth_environment, team, delivery)
    assert run_once(auth_environment.sessions, timeout_seconds=5) is False
    assert review_snapshot(auth_environment, team, delivery) == before
    progress = team.member.get(f"/api/projects/{team.project['id']}/deliveries").json()[0][
        "review_progress"
    ]
    assert progress["status"] == "failed" and progress["last_error"] == "pipeline_not_ready"
    assert "未完成" in progress["message"] and "尚无审核结论" in progress["message"]


def test_fixed_handler_executes_in_actual_child_and_success_does_not_enter_final_review(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    assert (
        run_once(
            auth_environment.sessions,
            handler=fixed_pid_handler,
            timeout_seconds=5,
            lease_grace_seconds=2,
        )
        is True
    )
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert job.status == "succeeded" and job.attempt == 1
        result = job.result
        assert result["pid"] != os.getpid() and result["parent_pid"] == os.getpid()
        assert result["delivery_id"] == delivery["id"]
        assert result["project_id"] == team.project["id"]
        assert result["attempt"] == result["workflow_revision"] == 1
        assert result["coverage"] == "not_covered"
        assert not process_exists(result["pid"])
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)
        assert_no_review_pass(db, delivery)
        completed = list(
            db.scalars(
                select(Event).where(
                    Event.object_id == delivery["id"], Event.event_type == "review_job_completed"
                )
            )
        )
        assert len(completed) == 1 and completed[0].actor_type == "agent"
        assert completed[0].actor_id == "platform-review-worker"
        assert completed[0].represented_user_id is None
        assert completed[0].details["execution_origin"] == "platform_worker"
        assert completed[0].details["initiated_by"] == team.member_id


def test_real_sleeping_child_is_hard_killed_on_timeout_and_never_submits_late_result(
    auth_environment,
    monkeypatch,
    tmp_path,
):
    _, delivery = queued_delivery(auth_environment)
    marker = tmp_path / "timeout-child.json"
    monkeypatch.setenv("TEAMS_TEST_WORKER_MARKER", str(marker))
    started = time.monotonic()
    assert (
        run_once(
            auth_environment.sessions,
            handler=marker_sleep_handler,
            timeout_seconds=3,
            lease_grace_seconds=3,
            retry_delay_seconds=0,
        )
        is True
    )
    elapsed = time.monotonic() - started
    child = wait_for_marker(marker, timeout=0.1)
    assert child["delivery_id"] == delivery["id"] and child["pid"] != os.getpid()
    assert not process_exists(child["pid"])
    assert 2.5 <= elapsed < 10
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt, job.last_error) == ("retry_wait", 1, "timeout")
        assert job.result is None and job.claim_token is None and job.lease_until is None
        assert db.get(DeliveryWorkflow, job.delivery_id).state == "ai_review_partial_failure"
        assert_no_review_pass(db, delivery)
        assert (
            list(
                db.scalars(
                    select(Event).where(
                        Event.object_id == delivery["id"],
                        Event.event_type == "review_job_completed",
                    )
                )
            )
            == []
        )


def test_stop_signal_kills_child_but_preserves_lease_for_fresh_worker_recovery(
    auth_environment,
    monkeypatch,
    tmp_path,
):
    team, delivery = queued_delivery(auth_environment)
    marker = tmp_path / "stopped-child.json"
    monkeypatch.setenv("TEAMS_TEST_WORKER_MARKER", str(marker))
    stopped = ThreadEvent()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_once,
            auth_environment.sessions,
            handler=marker_sleep_handler,
            timeout_seconds=20,
            lease_grace_seconds=2,
            stopped=stopped,
        )
        try:
            child = wait_for_marker(marker)
            assert process_exists(child["pid"])
            with auth_environment.sessions() as db:
                job = job_for(db, delivery)
                assert job.status == "running"
                original_token, lease_until = job.claim_token, job.lease_until
            stopped.set()
            assert future.result(timeout=8) is True
        finally:
            stopped.set()
    assert not process_exists(child["pid"])
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt) == ("running", 1)
        assert job.claim_token == original_token and job.lease_until == lease_until
        assert job.result is None and job.last_error is None
        assert db.get(DeliveryWorkflow, job.delivery_id).revision == 1
    database_url, options = database_binding(auth_environment)
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine.execution_options(**options), expire_on_commit=False)
    try:
        with sessions() as db:
            assert claim_next(db, now=lease_until - timedelta(microseconds=1)) is None
            db.commit()
        with sessions() as db:
            assert claim_next(db, now=lease_until, retry_delay_seconds=0) is None
            db.commit()
        with sessions() as db:
            restarted = claim_next(db, now=lease_until)
            db.commit()
        assert restarted is not None and restarted.attempt == 2
        assert restarted.claim_token != original_token and restarted.workflow_revision == 3
        with sessions() as db:
            assert complete_job(db, restarted, {"coverage": "not_covered"}, now=lease_until) is True
            db.commit()
    finally:
        engine.dispose()
    with auth_environment.sessions() as db:
        assert job_for(db, delivery).status == "succeeded"
        assert_no_review_pass(db, delivery)
        assert list(
            db.scalars(
                select(Event).where(
                    Event.object_id == delivery["id"],
                    Event.event_type == "review_job_lease_expired",
                )
            )
        )


def test_sigkill_after_claim_commit_is_recovered_in_a_different_real_worker_process(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    database_url, options = database_binding(auth_environment)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=claim_then_wait, args=(database_url, options, sender))
    process.start()
    sender.close()
    try:
        assert receiver.poll(10), "Claim worker did not commit and report its claim"
        first_pid, first = receiver.recv()
        assert first is not None and first.attempt == 1 and first_pid == process.pid
        with auth_environment.sessions() as db:
            job = job_for(db, delivery)
            assert job.status == "running" and job.claim_token == first.claim_token
            lease_until = job.lease_until
        os.kill(first_pid, signal.SIGKILL)
        process.join(timeout=5)
        assert process.exitcode == -signal.SIGKILL
        assert not process_exists(first_pid)
    finally:
        receiver.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()
    receiver, sender = context.Pipe(duplex=False)
    restarted_process = context.Process(
        target=recover_in_new_worker, args=(database_url, options, lease_until, sender)
    )
    restarted_process.start()
    sender.close()
    try:
        assert receiver.poll(10), "Replacement worker did not finish lease recovery"
        report = receiver.recv()
        restarted_process.join(timeout=5)
        assert restarted_process.exitcode == 0
        assert report["pid"] != first_pid and report["pid"] != os.getpid()
        assert report["early_claim"] is None and report["expiry_claim"] is None
        assert report["accepted"] is True
        second = report["restarted"]
        assert second.claim_token != first.claim_token and second.attempt == 2
        assert second.workflow_revision == 3
    finally:
        receiver.close()
        if restarted_process.is_alive():
            restarted_process.kill()
            restarted_process.join(timeout=5)
        restarted_process.close()
    after = review_snapshot(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        assert complete_job(db, first, {"late_old_worker": True}, now=lease_until) is False
        db.commit()
    assert review_snapshot(auth_environment, team, delivery) == after
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert job.status == "succeeded" and job.attempt == 2
        assert job.result["restarted_pid"] == report["pid"]
        assert_no_review_pass(db, delivery)
        history = list(
            db.scalars(
                select(DeliveryStateTransition)
                .where(DeliveryStateTransition.delivery_id == UUID(delivery["id"]))
                .order_by(DeliveryStateTransition.revision)
            )
        )
        assert [item.to_state for item in history] == [
            "ai_reviewing",
            "ai_review_partial_failure",
            "ai_reviewing",
        ]
        for name, expected in (
            ("review_job_claimed", 2),
            ("review_job_completed", 1),
            ("review_job_lease_expired", 1),
        ):
            assert (
                len(
                    list(
                        db.scalars(
                            select(Event).where(
                                Event.object_id == delivery["id"], Event.event_type == name
                            )
                        )
                    )
                )
                == expected
            )


@pytest.mark.parametrize("handler", [private_error_handler, oversized_handler, non_json_handler])
def test_child_failure_and_invalid_ipc_payload_are_fixed_errors_without_private_output(
    auth_environment,
    handler,
    capfd,
):
    team, delivery = queued_delivery(auth_environment)
    assert (
        run_once(
            auth_environment.sessions,
            handler=handler,
            timeout_seconds=5,
            lease_grace_seconds=2,
            retry_delay_seconds=0,
        )
        is True
    )
    output = capfd.readouterr()
    assert PRIVATE_EXCEPTION not in output.out and PRIVATE_EXCEPTION not in output.err
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.last_error) == ("retry_wait", "execution_error")
        assert job.result is None
        assert_no_review_pass(db, delivery)
    assert PRIVATE_EXCEPTION not in json.dumps(
        review_snapshot(auth_environment, team, delivery), default=str
    )
