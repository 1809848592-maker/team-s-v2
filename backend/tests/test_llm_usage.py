"""PostgreSQL LLM admission and accounting; no provider or network is contacted."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_deliveries import ready_team, upload_delivery
from test_modules import publish_modules, team_modules
from test_projects import accept, create_project, invite
from test_specs import publish_spec

from app.models import DeliveryWorkflow
from app.models.llm_usage import DailyBudget, LLMProjectPolicy, ModelCall, ReviewAdmission
from app.models.review_job import ReviewJob
from app.services import llm_usage
from app.services.llm_usage import (
    LLMCallLimitError,
    LLMDuplicateCallError,
    LLMUsageError,
    QuotaExceededError,
    QuotaLimits,
    reserve_call,
    settle_call,
)
from app.services.review_jobs import claim_next

DAY_ONE = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
PRICES = {
    "currency": "CNY",
    "input_cache_hit": "0.04",
    "input_cache_miss": "2",
    "output": "8",
    "effective_date": "2026-11-11",
    "estimate_basis": "peak_ceiling",
}
KNOWN_USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "total_tokens": 150,
    "prompt_cache_hit_tokens": 40,
    "prompt_cache_miss_tokens": 60,
}
ACTUAL_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


@pytest.fixture
def quota_clock(monkeypatch):
    clock = SimpleNamespace(now=DAY_ONE)
    monkeypatch.setattr(llm_usage, "_now", lambda _db: clock.now)
    return clock


def formal_job(environment, team):
    response = upload_delivery(team.member, team, b"fixed model accounting source")
    assert response.status_code == 201, response.text
    delivery = response.json()
    with environment.sessions() as db:
        job = db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == UUID(delivery["id"])))
        assert job is not None
        return SimpleNamespace(
            project_id=UUID(team.project["id"]), job_id=job.id, delivery_id=UUID(delivery["id"])
        )


def second_project(environment, team):
    # Existing test people join another real project; no fabricated foreign-key rows.
    other = SimpleNamespace(**vars(team))
    other.project = create_project(team.leader, "另一独立配额项目")
    for person in (other.member, other.other_member):
        assert accept(person, invite(other.leader, other.project["id"])).status_code == 200
    other.modules = team_modules(other)
    other.plan = publish_modules(other, other.modules)
    other.spec = publish_spec(other.leader, other.project["id"])
    return other


def reserve(environment, job, *, call_id=None, reviewer="format", reserved_tokens=300, **changes):
    identity = uuid4() if call_id is None else call_id
    values = {
        "project_id": job.project_id,
        "job_id": job.job_id,
        "call_id": identity,
        "reviewer": reviewer,
        "instruction_version": "test-instruction-v1",
        "model": "fixed-provider-model",
        "endpoint": "https://provider.invalid/v1/chat/completions",
        "input_hash": "a" * 64,
        "reserved_tokens": reserved_tokens,
        "prices": dict(PRICES),
    }
    values.update(changes)
    assert reserve_call(environment.sessions, **values) == identity
    return identity


def settle_usage(environment, call_id, *, status="succeeded", **values):
    return settle_call(environment.sessions, call_id, status=status, **values)


def budget(db, scope_key, day=DAY_ONE.date()):
    return db.scalar(
        select(DailyBudget).where(DailyBudget.scope_key == str(scope_key), DailyBudget.day == day)
    )


def account_snapshot(environment):
    with environment.sessions() as db:
        result = {}
        for model in (DailyBudget, LLMProjectPolicy, ModelCall, ReviewAdmission):
            result[model.__tablename__] = [
                dict(item)
                for item in db.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key.columns)
                ).mappings()
            ]
        return result


def assert_no_calls_or_admissions(environment):
    with environment.sessions() as db:
        assert list(db.scalars(select(ModelCall))) == []
        assert list(db.scalars(select(ReviewAdmission))) == []
        for item in db.scalars(select(DailyBudget)):
            assert item.review_count == item.reserved_tokens == item.charged_tokens == 0


def assert_unknown_call(db, call_id, reserved_tokens=300):
    item = db.get(ModelCall, call_id)
    assert item.status == "unknown"
    assert all(getattr(item, field) is None for field in ACTUAL_USAGE_FIELDS)
    assert item.estimated_cost_cny is None
    # Actual usage is unknown; the original reservation remains in the shared allowance.
    platform = budget(db, "platform", item.day)
    assert platform.reserved_tokens == reserved_tokens and platform.charged_tokens == 0
    return item


def test_four_reviewers_share_one_delivery_review_admission_but_have_individual_call_usage(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    calls = [
        reserve(auth_environment, job, reviewer=name)
        for name in ("specification", "citations", "logic", "handoff")
    ]
    with auth_environment.sessions() as db:
        admissions = list(db.scalars(select(ReviewAdmission)))
        assert len(admissions) == 1
        admission = admissions[0]
        assert admission.job_id == job.job_id and admission.project_id == job.project_id
        assert admission.day == DAY_ONE.date() and admission.call_count == 4
        records = list(db.scalars(select(ModelCall)))
        assert {item.id for item in records} == set(calls)
        assert {item.admission_id for item in records} == {admission.id}
        assert all(item.status == "reserved" for item in records)
        assert budget(db, "platform").review_count == 1
        assert budget(db, job.project_id).review_count == 1
        assert budget(db, "platform").reserved_tokens == 1200
        assert budget(db, "platform").charged_tokens == 0
        policy = db.get(LLMProjectPolicy, job.project_id)
        assert policy.daily_review_limit == 20 and policy.api_key_reference is None


def test_reservation_commits_independently_and_duplicate_call_does_not_charge_twice(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    with auth_environment.sessions() as unrelated:
        call_id = reserve(auth_environment, job)
        unrelated.rollback()
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, call_id).status == "reserved"
    before = account_snapshot(auth_environment)
    with pytest.raises(LLMDuplicateCallError):
        reserve(auth_environment, job, call_id=call_id)
    assert account_snapshot(auth_environment) == before


def test_two_projects_contending_for_final_platform_token_allowance_reserve_exactly_one(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    jobs = [formal_job(auth_environment, team)]
    jobs.append(formal_job(auth_environment, second_project(auth_environment, team)))
    limits = QuotaLimits(platform_daily_tokens=100)
    barrier = Barrier(2)

    def compete(job):
        barrier.wait(timeout=10)
        try:
            return "reserved", reserve(auth_environment, job, reserved_tokens=60, limits=limits)
        except QuotaExceededError as error:
            return "denied", error.scope

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, job) for job in jobs]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(status for status, _ in outcomes) == ["denied", "reserved"]
    assert next(value for status, value in outcomes if status == "denied") == "platform_tokens"
    with auth_environment.sessions() as db:
        platform = budget(db, "platform")
        assert (platform.review_count, platform.reserved_tokens, platform.charged_tokens) == (
            1,
            60,
            0,
        )
        calls = list(db.scalars(select(ModelCall)))
        admissions = list(db.scalars(select(ReviewAdmission)))
        assert len(calls) == len(admissions) == 1
        assert calls[0].project_id == admissions[0].project_id


def test_two_formal_deliveries_contending_for_last_project_review_admit_only_one(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    jobs = [formal_job(auth_environment, team), formal_job(auth_environment, team)]
    limits = QuotaLimits(project_daily_reviews=1)
    barrier = Barrier(2)

    def compete(job):
        barrier.wait(timeout=10)
        try:
            return "reserved", reserve(auth_environment, job, limits=limits)
        except QuotaExceededError as error:
            return "denied", error.scope

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, job) for job in jobs]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(status for status, _ in outcomes) == ["denied", "reserved"]
    assert next(value for status, value in outcomes if status == "denied") == "project_reviews"
    with auth_environment.sessions() as db:
        assert budget(db, jobs[0].project_id).review_count == 1
        assert budget(db, "platform").review_count == 1
        assert len(list(db.scalars(select(ReviewAdmission)))) == 1


def test_platform_review_limit_applies_across_projects_independently_of_per_call_tokens(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    first = formal_job(auth_environment, team)
    second = formal_job(auth_environment, second_project(auth_environment, team))
    limits = QuotaLimits(platform_daily_reviews=1)
    reserve(auth_environment, first, limits=limits)
    before = account_snapshot(auth_environment)
    with pytest.raises(QuotaExceededError) as denied:
        reserve(auth_environment, second, limits=limits)
    assert denied.value.scope == "platform_reviews"
    assert denied.value.retry_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert account_snapshot(auth_environment) == before


def test_zero_project_limit_pauses_before_any_model_call_or_admission(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    with pytest.raises(QuotaExceededError) as denied:
        reserve(auth_environment, job, limits=QuotaLimits(project_daily_reviews=0))
    assert denied.value.scope == "project_reviews"
    assert denied.value.retry_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert_no_calls_or_admissions(auth_environment)
    with auth_environment.sessions() as db:
        workflow = db.get(DeliveryWorkflow, job.delivery_id)
        assert (workflow.state, workflow.revision) == ("ai_reviewing", 1)


def test_review_call_limit_counts_calls_without_spending_another_daily_review(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    limits = QuotaLimits(max_calls_per_review=2)
    reserve(auth_environment, job, reviewer="first", limits=limits)
    reserve(auth_environment, job, reviewer="second", limits=limits)
    before = account_snapshot(auth_environment)
    with pytest.raises(LLMCallLimitError):
        reserve(auth_environment, job, reviewer="third", limits=limits)
    assert account_snapshot(auth_environment) == before
    with auth_environment.sessions() as db:
        assert budget(db, job.project_id).review_count == budget(db, "platform").review_count == 1
        assert db.scalar(select(ReviewAdmission)).call_count == 2


def test_known_usage_settles_once_releases_reservation_difference_and_estimates_cache_cost(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    assert (
        settle_usage(auth_environment, call_id, usage=dict(KNOWN_USAGE), output_hash="b" * 64)
        is True
    )
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, call_id)
        assert call.status == "succeeded"
        assert tuple(getattr(call, field) for field in ACTUAL_USAGE_FIELDS) == (
            100,
            50,
            150,
            40,
            60,
        )
        assert call.charged_tokens == 150 and call.reserved_tokens == 300
        assert call.estimated_cost_cny == Decimal("0.0005216")
        assert call.prices == PRICES and call.output_hash == "b" * 64
        assert call.input_hash == "a" * 64
        assert budget(db, "platform").reserved_tokens == 0
        assert budget(db, "platform").charged_tokens == 150
    before = account_snapshot(auth_environment)
    assert (
        settle_usage(
            auth_environment,
            call_id,
            usage={**KNOWN_USAGE, "completion_tokens": 99},
            status="failed",
            error_code="timeout",
            output_hash="c" * 64,
        )
        is False
    )
    assert account_snapshot(auth_environment) == before


def test_concurrent_settlement_for_same_call_charges_exactly_once(auth_environment, quota_clock):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    barrier = Barrier(2)

    def settle(output):
        barrier.wait(timeout=10)
        return output, settle_usage(
            auth_environment, call_id, usage=dict(KNOWN_USAGE), output_hash=output * 64
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(settle, output) for output in ("b", "c")]
        outcomes = [future.result(timeout=20) for future in futures]
    assert sorted(accepted for _, accepted in outcomes) == [False, True]
    winner = next(output for output, accepted in outcomes if accepted)
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, call_id).output_hash == winner * 64
        assert budget(db, "platform").charged_tokens == 150
        assert budget(db, "platform").reserved_tokens == 0


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_missing_usage_is_unknown_and_never_refunds_reserved_tokens_or_invents_zero(
    auth_environment,
    quota_clock,
    status,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    assert (
        settle_usage(
            auth_environment,
            call_id,
            usage=None,
            status=status,
            error_code="timeout" if status == "failed" else None,
        )
        is True
    )
    with auth_environment.sessions() as db:
        unknown = assert_unknown_call(db, call_id)
        assert unknown.error_code == ("timeout" if status == "failed" else "usage_missing")
    before = account_snapshot(auth_environment)
    assert settle_usage(auth_environment, call_id, usage=dict(KNOWN_USAGE)) is False
    assert account_snapshot(auth_environment) == before


def test_known_failed_call_still_charges_actual_provider_usage(auth_environment, quota_clock):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    assert (
        settle_usage(
            auth_environment,
            call_id,
            usage=dict(KNOWN_USAGE),
            status="failed",
            error_code="timeout",
        )
        is True
    )
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, call_id)
        assert call.status == "failed" and call.error_code == "timeout"
        assert call.total_tokens == call.charged_tokens == 150
        assert budget(db, "platform").charged_tokens == 150
        assert budget(db, "platform").reserved_tokens == 0


def test_known_usage_without_cache_breakdown_is_recorded_without_fabricating_cost(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    usage = {
        key: KNOWN_USAGE[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    assert settle_usage(auth_environment, call_id, usage=usage) is True
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, call_id)
        assert call.total_tokens == call.charged_tokens == 150
        assert call.cache_hit_tokens is None and call.cache_miss_tokens is None
        assert call.estimated_cost_cny is None
        assert budget(db, "platform").charged_tokens == 150


def test_committed_reservation_survives_new_pool_and_blocks_oversubscription_after_worker_crash(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    first = formal_job(auth_environment, team)
    second = formal_job(auth_environment, team)
    limits = QuotaLimits(platform_daily_tokens=400)
    call_id = reserve(auth_environment, first, limits=limits)
    with auth_environment.sessions() as db:
        bind = db.get_bind()
        database_url = bind.url
        options = dict(bind.get_execution_options())
    # The original worker vanished after commit; a fresh pool must honor its durable reservation.
    engine = create_engine(database_url, pool_pre_ping=True)
    sessions = sessionmaker(bind=engine.execution_options(**options), expire_on_commit=False)
    try:
        with sessions() as db:
            call = db.get(ModelCall, call_id)
            assert call.status == "reserved" and call.total_tokens is None
            assert budget(db, "platform").reserved_tokens == 300
        with pytest.raises(QuotaExceededError) as denied:
            reserve_call(
                sessions,
                project_id=second.project_id,
                job_id=second.job_id,
                call_id=uuid4(),
                reviewer="logic",
                instruction_version="v1",
                model="fixed-provider-model",
                endpoint="https://provider.invalid/v1/chat/completions",
                input_hash="d" * 64,
                reserved_tokens=200,
                prices=PRICES,
                limits=limits,
            )
        assert denied.value.scope == "platform_tokens"
    finally:
        engine.dispose()
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, call_id).status == "reserved"
        assert budget(db, "platform").charged_tokens == 0
    # A later known provider outcome releases only the proven unused difference.
    assert settle_usage(auth_environment, call_id, usage=dict(KNOWN_USAGE)) is True
    reserve(auth_environment, second, reserved_tokens=200, limits=limits)
    with auth_environment.sessions() as db:
        assert budget(db, "platform").charged_tokens == 150
        assert budget(db, "platform").reserved_tokens == 200


def test_midnight_settlement_uses_original_call_day_while_same_review_new_call_uses_next_day(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    quota_clock.now = datetime(2026, 11, 11, 23, 59, 59, tzinfo=UTC)
    first = reserve(auth_environment, job)
    quota_clock.now += timedelta(seconds=2)
    next_day = quota_clock.now.date()
    assert settle_usage(auth_environment, first, usage=dict(KNOWN_USAGE)) is True
    second = reserve(auth_environment, job, reviewer="logic")
    fresh_job = formal_job(auth_environment, team)
    third = reserve(auth_environment, fresh_job)
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, first).day == DAY_ONE.date()
        assert db.get(ModelCall, second).day == db.get(ModelCall, third).day == next_day
        original_admission = db.scalar(
            select(ReviewAdmission).where(ReviewAdmission.job_id == job.job_id)
        )
        assert original_admission.day == DAY_ONE.date() and original_admission.call_count == 2
        assert budget(db, "platform", DAY_ONE.date()).review_count == 1
        assert budget(db, job.project_id, DAY_ONE.date()).review_count == 1
        assert budget(db, "platform", DAY_ONE.date()).charged_tokens == 150
        assert budget(db, "platform", DAY_ONE.date()).reserved_tokens == 0
        assert budget(db, "platform", next_day).review_count == 1
        assert budget(db, job.project_id, next_day).review_count == 1
        assert budget(db, "platform", next_day).reserved_tokens == 600
        assert budget(db, "platform", next_day).charged_tokens == 0


def test_usage_is_charged_even_after_original_worker_lease_has_become_stale(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    with auth_environment.sessions() as db:
        claimed = claim_next(db, timeout_seconds=1, lease_grace_seconds=0)
        assert claimed is not None and claimed.job_id == job.job_id
        db.commit()
        lease_until = db.get(ReviewJob, job.job_id).lease_until
    call_id = reserve(auth_environment, job)
    with auth_environment.sessions() as db:
        assert claim_next(db, now=lease_until, retry_delay_seconds=0) is None
        db.commit()
    assert settle_usage(auth_environment, call_id, usage=dict(KNOWN_USAGE)) is True
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, call_id).charged_tokens == 150
        assert budget(db, "platform").charged_tokens == 150
        assert db.get(ReviewJob, job.job_id).status == "retry_wait"


def test_forged_job_or_wrong_project_is_rejected_before_creating_an_accounting_record(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    other = second_project(auth_environment, team)
    for changes in ({"job_id": uuid4()}, {"project_id": UUID(other.project["id"])}):
        with pytest.raises(LLMUsageError):
            reserve(auth_environment, job, **changes)
        assert_no_calls_or_admissions(auth_environment)


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": -1, "completion_tokens": 2, "total_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 1.0, "completion_tokens": 2, "total_tokens": 3},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99},
        {"prompt_tokens": 1, "completion_tokens": False, "total_tokens": 1},
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": "3"},
    ],
)
def test_malformed_usage_becomes_unknown_with_reservation_held(
    auth_environment, quota_clock, usage
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    assert settle_usage(auth_environment, call_id, usage=usage) is True
    with auth_environment.sessions() as db:
        assert assert_unknown_call(db, call_id).error_code == "usage_invalid"


def test_openai_cached_tokens_detail_derives_consistent_cache_miss_tokens(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    call_id = reserve(auth_environment, job)
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    assert settle_usage(auth_environment, call_id, usage=usage) is True
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, call_id)
        assert call.status == "succeeded" and call.total_tokens == 150
        assert call.cache_hit_tokens == 40 and call.cache_miss_tokens == 60
        assert call.estimated_cost_cny == Decimal("0.0005216")


def test_concurrent_reviewers_of_same_job_create_one_admission_and_four_calls(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    barrier = Barrier(4)

    def same_review(index):
        barrier.wait(timeout=10)
        return reserve(auth_environment, job, reviewer=f"reviewer-{index}")

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(same_review, index) for index in range(4)]
        ids = [future.result(timeout=20) for future in futures]
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        admissions = list(db.scalars(select(ReviewAdmission)))
        assert len(admissions) == 1 and admissions[0].call_count == 4
        assert len(calls) == 4 and {item.id for item in calls} == set(ids)
        assert {item.admission_id for item in calls} == {admissions[0].id}
        assert budget(db, "platform").review_count == budget(db, job.project_id).review_count == 1
        assert budget(db, "platform").reserved_tokens == 1200


def test_per_review_call_cap_cannot_be_zero_but_daily_quotas_can_pause_all_calls():
    with pytest.raises(ValueError):
        QuotaLimits(max_calls_per_review=0)
    assert QuotaLimits(project_daily_reviews=0).project_daily_reviews == 0
    assert QuotaLimits(platform_daily_reviews=0).platform_daily_reviews == 0
    assert QuotaLimits(platform_daily_tokens=0).platform_daily_tokens == 0
