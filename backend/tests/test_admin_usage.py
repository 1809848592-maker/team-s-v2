"""M3-13 管理员账本汇总、精确导出、额度原子更新的真实 PG 回归。"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_deliveries import ready_team
from test_llm_usage import (
    DAY_ONE,
    KNOWN_USAGE,
    formal_job,
    reserve,
    second_project,
    settle_usage,
)
from test_llm_usage import quota_clock as quota_clock

from app.config import Settings
from app.models import Event, Project
from app.models.llm_usage import LLMProjectPolicy, ModelCall
from app.services import admin_usage
from app.services.llm_usage import QuotaExceededError


def settings_for(identity, **changes):
    return Settings(_env_file=None, platform_admin_user_ids=str(identity), **changes)


def dataset(environment, clock):
    team = ready_team(environment)
    other = second_project(environment, team)
    first_job, other_job = formal_job(environment, team), formal_job(environment, other)
    first = reserve(environment, first_job, reviewer="logic")
    settle_usage(environment, first, usage=dict(KNOWN_USAGE))
    failed = reserve(environment, first_job, reviewer="spec_compliance")
    settle_usage(
        environment, failed, usage=dict(KNOWN_USAGE), status="failed", error_code="timeout"
    )
    unknown = reserve(environment, first_job, reviewer="logic")
    settle_usage(environment, unknown, usage=None, status="failed", error_code="timeout")
    waiting = reserve(environment, first_job, reviewer="logic")
    no_cache = reserve(environment, other_job, reviewer="logic")
    settle_usage(
        environment,
        no_cache,
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    )
    clock.now += timedelta(days=1)
    later = reserve(environment, other_job, reviewer="linkage")
    settle_usage(environment, later, usage=dict(KNOWN_USAGE))
    return team, other, [first, failed, unknown, waiting, no_cache, later]


def report(environment, team, start=DAY_ONE.date(), end=DAY_ONE.date(), **changes):
    with environment.sessions() as db:
        return admin_usage.usage_report(
            db,
            UUID(team.leader_id),
            settings_for(team.leader_id),
            start,
            end,
            **changes,
        )


def test_summary_sums_known_usage_and_precise_recorded_cost_without_inventing_unknown(
    auth_environment,
    quota_clock,
):
    team, other, calls = dataset(auth_environment, quota_clock)
    result = report(auth_environment, team)
    totals = result.totals
    assert totals.calls == 5 and totals.measured_calls == 3 and totals.unknown_usage_calls == 2
    assert totals.unknown_cache_calls == totals.unpriced_calls == 3
    assert (totals.prompt_tokens, totals.completion_tokens, totals.total_tokens) == (210, 105, 315)
    assert (totals.cache_hit_tokens, totals.cache_miss_tokens) == (80, 120)
    assert totals.estimated_cost_cny == "0.0010432000"
    assert result.timezone == "UTC" and len(result.by_project) == len(result.by_reviewer) == 2
    assert sum(item.total_tokens for item in result.by_project) == totals.total_tokens
    assert sum(item.total_tokens for item in result.by_reviewer) == totals.total_tokens
    assert sum(Decimal(item.estimated_cost_cny) for item in result.by_project) == Decimal(
        totals.estimated_cost_cny
    )
    assert {item.project_id for item in result.by_project} == {
        UUID(team.project["id"]),
        UUID(other.project["id"]),
    }
    assert result.limits.platform_daily_tokens == 1_000_000
    payload = result.model_dump_json()
    assert "provider.invalid" not in payload and "input_hash" not in payload
    with auth_environment.sessions() as db:
        assert db.get(ModelCall, calls[3]).status == "reserved"


def test_date_boundaries_project_filter_and_zero_known_usage(auth_environment, quota_clock):
    team, other, _ = dataset(auth_environment, quota_clock)
    tomorrow = DAY_ONE.date() + timedelta(days=1)
    assert report(auth_environment, team, end=tomorrow).totals.total_tokens == 465
    filtered = report(auth_environment, team, project_id=UUID(other.project["id"]))
    assert filtered.totals.total_tokens == 15 and len(filtered.by_project) == 1
    assert report(auth_environment, team, start=tomorrow, end=tomorrow).totals.total_tokens == 150
    assert (
        report(
            auth_environment,
            team,
            start=tomorrow + timedelta(days=1),
            end=tomorrow + timedelta(days=1),
        ).totals.calls
        == 0
    )
    call = reserve(auth_environment, formal_job(auth_environment, team))
    settle_usage(
        auth_environment,
        call,
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
        },
    )
    zero_day = report(auth_environment, team, start=tomorrow, end=tomorrow)
    assert zero_day.totals.measured_calls == 2 and zero_day.totals.unknown_usage_calls == 0
    assert zero_day.totals.total_tokens == 150 and zero_day.totals.unpriced_calls == 0


def test_csv_detail_matches_ledger_and_sums_and_keeps_unknown_fields_empty(
    auth_environment,
    quota_clock,
):
    team, _, calls = dataset(auth_environment, quota_clock)
    with auth_environment.sessions() as db:
        content = admin_usage.usage_csv(
            db,
            UUID(team.leader_id),
            settings_for(team.leader_id),
            DAY_ONE.date(),
            DAY_ONE.date(),
        )
        recorded = {
            str(item.id): item
            for item in db.scalars(select(ModelCall).where(ModelCall.day == DAY_ONE.date()))
        }
    assert content.startswith("\ufeff")
    rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    assert len(rows) == 5 and {row["call_id"] for row in rows} == set(recorded)
    for row in rows:
        item = recorded[row["call_id"]]
        for field in admin_usage.TOKEN_FIELDS:
            expected = getattr(item, field)
            assert row[field] == ("" if expected is None else str(expected))
        assert row["status"] == item.status
        assert row["estimated_cost_cny"] == (
            "" if item.estimated_cost_cny is None else format(item.estimated_cost_cny, ".10f")
        )
    summary = report(auth_environment, team).totals
    assert (
        sum(int(row["total_tokens"]) for row in rows if row["total_tokens"]) == summary.total_tokens
    )
    assert sum(
        Decimal(row["estimated_cost_cny"]) for row in rows if row["estimated_cost_cny"]
    ) == Decimal(summary.estimated_cost_cny)
    assert next(row for row in rows if row["call_id"] == str(calls[2]))["total_tokens"] == ""
    assert "input_hash" not in content and "provider.invalid" not in content


@pytest.mark.parametrize(
    "value", ["=SUM(A1)", "+FORMULA", "-1+2", "@evil", " \t=evil", "\nordinary"]
)
def test_csv_neutralizes_spreadsheet_formula_cells(value):
    assert admin_usage._csv_cell(value) == "'" + value


def test_exported_database_names_and_model_metadata_cannot_become_formulas(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    with auth_environment.sessions() as db:
        db.get(Project, UUID(team.project["id"])).name = "=SUM(A1:A2)"
        db.commit()
    job = formal_job(auth_environment, team)
    identity = reserve(auth_environment, job, reviewer=" \t@reviewer", model="+formula-model")
    settle_usage(auth_environment, identity, usage=dict(KNOWN_USAGE))
    with auth_environment.sessions() as db:
        content = admin_usage.usage_csv(
            db, UUID(team.leader_id), settings_for(team.leader_id), DAY_ONE.date(), DAY_ONE.date()
        )
    row = next(csv.DictReader(io.StringIO(content.lstrip("\ufeff"))))
    assert row["project_name"] == "'=SUM(A1:A2)"
    assert row["reviewer"] == "' \t@reviewer"
    assert row["model"] == "'+formula-model"
    assert row["total_tokens"] == "150"


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2026, 11, 12), date(2026, 11, 11)),
        (date(2025, 1, 1), date(2026, 1, 2)),
    ],
)
def test_invalid_ranges_are_rejected_not_truncated(auth_environment, start, end):
    team = ready_team(auth_environment)
    with pytest.raises(admin_usage.UsageRangeError):
        report(auth_environment, team, start, end)


def test_row_limit_fails_summary_and_csv_instead_of_partial_export(
    auth_environment,
    quota_clock,
    monkeypatch,
):
    team, _, _ = dataset(auth_environment, quota_clock)
    monkeypatch.setattr(admin_usage, "MAX_REPORT_CALLS", 2)
    for operation in (admin_usage.usage_report, admin_usage.usage_csv):
        with auth_environment.sessions() as db, pytest.raises(admin_usage.UsageRangeError):
            operation(
                db,
                UUID(team.leader_id),
                settings_for(team.leader_id),
                DAY_ONE.date(),
                DAY_ONE.date(),
            )


def test_admin_service_permissions_do_not_depend_on_project_leader_role(auth_environment):
    team = ready_team(auth_environment)
    settings = settings_for(team.member_id)
    with auth_environment.sessions() as db:
        for operation in (
            lambda: admin_usage.list_projects(db, UUID(team.leader_id), settings),
            lambda: admin_usage.usage_report(
                db, UUID(team.leader_id), settings, DAY_ONE.date(), DAY_ONE.date()
            ),
            lambda: admin_usage.usage_csv(
                db, UUID(team.leader_id), settings, DAY_ONE.date(), DAY_ONE.date()
            ),
            lambda: admin_usage.update_project_quota(
                db,
                UUID(team.leader_id),
                settings,
                UUID(team.project["id"]),
                {"daily_review_limit": 0},
            ),
        ):
            with pytest.raises(admin_usage.AdminRequiredError):
                operation()
        assert list(db.scalars(select(LLMProjectPolicy))) == []


def test_effective_default_override_quota_and_audit_immediately_pause_new_admissions(
    auth_environment,
    quota_clock,
):
    team = ready_team(auth_environment)
    settings = settings_for(team.leader_id, llm_project_daily_reviews=9)
    project_id, actor = UUID(team.project["id"]), UUID(team.leader_id)
    with auth_environment.sessions() as db:
        before = admin_usage.list_projects(db, actor, settings)
        assert before[0].daily_review_limit == 9
        assert (
            admin_usage.update_project_quota(
                db, actor, settings, project_id, {"daily_review_limit": 0}
            ).daily_review_limit
            == 0
        )
        assert admin_usage.list_projects(db, actor, settings)[0].daily_review_limit == 0
        events = list(
            db.scalars(select(Event).where(Event.event_type == "llm_project_quota_updated"))
        )
        assert len(events) == 1 and events[0].actor_id == team.leader_id
        assert events[0].details == {"old_daily_review_limit": 9, "daily_review_limit": 0}
    job = formal_job(auth_environment, team)
    with pytest.raises(QuotaExceededError) as denied:
        reserve(auth_environment, job)
    assert denied.value.scope == "project_reviews"
    with auth_environment.sessions() as db:
        admin_usage.update_project_quota(db, actor, settings, project_id, {"daily_review_limit": 2})
    reserve(auth_environment, job)


def test_quota_late_event_failure_rolls_back_new_policy_and_event(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    actual = admin_usage.record_event

    def fail(*args, **kwargs):
        actual(*args, **kwargs)
        raise RuntimeError("fixed event failure")

    monkeypatch.setattr(admin_usage, "record_event", fail)
    with auth_environment.sessions() as db, pytest.raises(RuntimeError):
        admin_usage.update_project_quota(
            db,
            UUID(team.leader_id),
            settings_for(team.leader_id),
            UUID(team.project["id"]),
            {"daily_review_limit": 0},
        )
    with auth_environment.sessions() as db:
        assert db.get(LLMProjectPolicy, UUID(team.project["id"])) is None
        assert not list(
            db.scalars(select(Event).where(Event.event_type == "llm_project_quota_updated"))
        )


def test_concurrent_admin_quota_changes_are_serialized_and_audited(auth_environment):
    team = ready_team(auth_environment)
    settings, project_id = settings_for(team.leader_id), UUID(team.project["id"])

    def change(limit):
        with auth_environment.sessions() as db:
            return admin_usage.update_project_quota(
                db, UUID(team.leader_id), settings, project_id, {"daily_review_limit": limit}
            ).daily_review_limit

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(change, (5, 8)))
    assert sorted(results) == [5, 8]
    with auth_environment.sessions() as db:
        events = list(
            db.scalars(
                select(Event)
                .where(Event.event_type == "llm_project_quota_updated")
                .order_by(Event.id)
            )
        )
        assert len(events) == 2
        assert (
            events[1].details["old_daily_review_limit"] == events[0].details["daily_review_limit"]
        )
        assert (
            db.get(LLMProjectPolicy, project_id).daily_review_limit
            == events[-1].details["daily_review_limit"]
        )


def test_missing_project_errors_do_not_create_policy(auth_environment):
    team = ready_team(auth_environment)
    settings, actor = settings_for(team.leader_id), UUID(team.leader_id)
    with auth_environment.sessions() as db:
        with pytest.raises(admin_usage.AdminProjectNotFoundError):
            admin_usage.usage_report(db, actor, settings, DAY_ONE.date(), DAY_ONE.date(), uuid4())
        with pytest.raises(admin_usage.AdminProjectNotFoundError):
            admin_usage.update_project_quota(
                db, actor, settings, uuid4(), {"daily_review_limit": 0}
            )
        assert not list(db.scalars(select(LLMProjectPolicy)))


def test_maximum_date_window_is_accepted_and_exact_amount_is_not_repriced(
    auth_environment, quota_clock
):
    team = ready_team(auth_environment)
    identity = reserve(auth_environment, formal_job(auth_environment, team))
    settle_usage(auth_environment, identity, usage=dict(KNOWN_USAGE))
    # 报表使用历史账本金额，不以当前模型价格重算历史。
    with auth_environment.sessions() as db:
        row = db.get(ModelCall, identity)
        row.estimated_cost_cny = Decimal("1234567890.1234567890")
        db.commit()
    end = DAY_ONE.date() + timedelta(days=365)
    result = report(auth_environment, team, end=end)
    assert result.totals.estimated_cost_cny == "1234567890.1234567890"
    with pytest.raises(admin_usage.UsageRangeError):
        report(auth_environment, team, end=end + timedelta(days=1))


def test_failed_existing_policy_update_preserves_override_and_private_reference(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    project_id, actor = UUID(team.project["id"]), UUID(team.leader_id)
    settings = settings_for(actor)
    with auth_environment.sessions() as db:
        db.add(
            LLMProjectPolicy(
                project_id=project_id,
                daily_review_limit=7,
                api_key_reference="private-reference-only",
            )
        )
        db.commit()
    actual = admin_usage.record_event

    def fail(*args, **kwargs):
        actual(*args, **kwargs)
        raise RuntimeError("fixed late failure")

    monkeypatch.setattr(admin_usage, "record_event", fail)
    with auth_environment.sessions() as db, pytest.raises(RuntimeError):
        admin_usage.update_project_quota(db, actor, settings, project_id, {"daily_review_limit": 1})
    with auth_environment.sessions() as db:
        assert db.get(LLMProjectPolicy, project_id).daily_review_limit == 7
        assert db.get(LLMProjectPolicy, project_id).api_key_reference == "private-reference-only"
        assert "private-reference-only" not in str(admin_usage.list_projects(db, actor, settings))
        assert not list(
            db.scalars(select(Event).where(Event.event_type == "llm_project_quota_updated"))
        )
