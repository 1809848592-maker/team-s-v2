"""真实worker暂停与跨日恢复；只有固定处理器，无外部模型。"""

from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from uuid import UUID

import pytest
from sqlalchemy import select
from test_review_jobs import (
    assert_no_review_pass,
    claim,
    database_time,
    fail,
    finish,
    job_for,
    queued_delivery,
    review_snapshot,
)

from app.config import Settings
from app.models import DeliveryWorkflow, Event
from app.review.factory import build_llm_client
from app.review.llm import LLMDisabledError
from app.services.llm_usage import QuotaExceededError
from app.services.review_jobs import claim_next, fail_job
from app.worker import _run_handler, run_once


def fixed_quota_handler(review):
    now = datetime.now(UTC)
    raise QuotaExceededError(
        "platform_tokens",
        datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC),
    )


def fixed_midnight_quota_handler(review):
    raise QuotaExceededError("platform_tokens", datetime(2026, 11, 12, tzinfo=UTC))


def test_spawn_carries_original_utc_reset_even_when_parent_may_cross_midnight():
    outcome = _run_handler(fixed_midnight_quota_handler, None, 5, ThreadEvent())
    assert outcome.error_code == "quota_exceeded"
    assert outcome.retry_at == datetime(2026, 11, 12, tzinfo=UTC)


def test_real_spawn_pauses_without_retry_charge_or_conclusion_and_reports_safe_progress(
    auth_environment,
):
    team, delivery = queued_delivery(auth_environment)
    assert run_once(
        auth_environment.sessions,
        handler=fixed_quota_handler,
        timeout_seconds=5,
        lease_grace_seconds=2,
        max_attempts=1,
    )
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.last_error, job.attempt) == ("paused", "quota_exceeded", 0)
        assert job.claim_token is job.lease_until is job.result is job.finished_at is None
        assert job.available_at > job.updated_at and job.available_at.hour == 0
        workflow = db.get(DeliveryWorkflow, UUID(delivery["id"]))
        assert (workflow.state, workflow.revision) == ("ai_review_partial_failure", 2)
        assert_no_review_pass(db, delivery)
        event = db.scalar(
            select(Event).where(
                Event.object_id == delivery["id"], Event.event_type == "review_job_paused"
            )
        )
        assert event.actor_type == "agent" and event.actor_id == "platform-review-worker"
        assert event.represented_user_id is None
        assert event.details["execution_origin"] == "platform_worker"
        assert event.details["initiated_by"] == team.member_id
        assert str(event.plan_version_id) == delivery["plan_version_id"]
        assert str(event.spec_version_id) == delivery["spec_version_id"]
    response = team.member.get(f"/api/projects/{team.project['id']}/deliveries")
    progress = response.json()[0]["review_progress"]
    assert progress["status"] == "paused" and progress["last_error"] == "quota_exceeded"
    assert "尚无审核结论" in progress["message"] and progress["next_attempt_at"]
    assert {"claim_token", "lease_until", "result"}.isdisjoint(progress)
    before = review_snapshot(auth_environment, team, delivery)
    assert run_once(auth_environment.sessions, handler=fixed_quota_handler) is False
    assert review_snapshot(auth_environment, team, delivery) == before


def test_paused_job_waits_until_utc_reset_then_claims_and_fences_old_worker(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    first = claim(auth_environment, now, max_attempts=1)
    reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    assert fail(auth_environment, first, now, "quota_exceeded", retry_at=reset)
    paused = review_snapshot(auth_environment, team, delivery)
    assert not finish(auth_environment, first, now)
    assert not fail(auth_environment, first, now, "timeout")
    assert review_snapshot(auth_environment, team, delivery) == paused
    assert claim(auth_environment, reset - timedelta(microseconds=1)) is None
    second = claim(auth_environment, reset, max_attempts=1)
    assert second.attempt == first.attempt == 1
    assert second.claim_token != first.claim_token
    assert second.workflow_revision == first.workflow_revision + 2
    assert not finish(auth_environment, first, reset)
    assert fail(auth_environment, second, reset, "timeout")
    with auth_environment.sessions() as db:
        job = job_for(db, delivery)
        assert (job.status, job.attempt, job.max_attempts) == ("failed", 1, 1)
        assert_no_review_pass(db, delivery)


def test_pause_is_transactional_and_rolls_back_with_its_workflow_and_events(auth_environment):
    team, delivery = queued_delivery(auth_environment)
    now = database_time(auth_environment)
    first = claim(auth_environment, now)
    before = review_snapshot(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        assert fail_job(db, first, "quota_exceeded", now=now)
        db.rollback()
    assert review_snapshot(auth_environment, team, delivery) == before
    with auth_environment.sessions() as db:
        with pytest.raises(ValueError, match="时区"):
            fail_job(db, first, "quota_exceeded", retry_at=datetime(2026, 11, 12), now=now)
    assert review_snapshot(auth_environment, team, delivery) == before
    with auth_environment.sessions() as db:
        assert claim_next(db, now=now) is None


def test_application_factory_passes_secret_limits_and_keeps_live_calls_disabled_by_default():
    settings = Settings(
        _env_file=None,
        deepseek_api_key="fixed-test-key-only",
        llm_enable_live_calls=False,
        llm_project_daily_reviews=3,
        llm_platform_daily_reviews=7,
        llm_platform_daily_tokens=10000,
        llm_max_calls_per_review=9,
    )
    client = build_llm_client(None, settings)
    assert "fixed-test-key-only" not in repr(settings)
    with pytest.raises(LLMDisabledError):
        client.complete_json(
            project_id=UUID(int=1),
            job_id=UUID(int=2),
            reviewer="logic",
            instruction_version="fixed-v1",
            messages=[{"role": "user", "content": "json {}"}],
        )
    assert client._quota_limits.project_daily_reviews == 3
    assert client._quota_limits.platform_daily_reviews == 7
    assert client._quota_limits.platform_daily_tokens == 10000
    assert client._quota_limits.max_calls_per_review == 9
