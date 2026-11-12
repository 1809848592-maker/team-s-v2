"""Project review sources share quotas and usage; providers are always fixed substitutes."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from test_deliveries import ready_team
from test_llm_transport import MESSAGES, USAGE, Ledger, response_body
from test_llm_usage import KNOWN_USAGE, PRICES, account_snapshot, formal_job, reserve

from app.config import Settings
from app.models import Event, ProjectMember
from app.models.llm_usage import DailyBudget, LLMProjectPolicy, ModelCall, ReviewAdmission
from app.review import llm
from app.review.llm import LLMClient, LLMRequestError
from app.services import admin_usage, model_disclosure
from app.services.llm_usage import (
    LLMCallLimitError,
    LLMDuplicateCallError,
    LLMUsageError,
    QuotaExceededError,
    QuotaLimits,
    reserve_call,
    settle_call,
)
from app.services.projects import LeaderRequiredError

FAKE_KEY = "project-review-fixed-test-key"


def project_source(environment, team):
    from app.models.project_linkage import ProjectLinkageReview

    now = datetime.now(UTC)
    with environment.sessions() as db:
        source = ProjectLinkageReview(
            id=uuid4(),
            project_id=UUID(team.project["id"]),
            requested_by=UUID(team.leader_id),
            plan_version_id=UUID(team.plan["id"]),
            spec_version_id=UUID(team.spec["id"]),
            frozen_input={},
            status="running",
            attempt=1,
            max_attempts=3,
            claim_token=uuid4(),
            lease_until=now + timedelta(minutes=10),
            available_at=now,
        )
        db.add(source)
        db.commit()
        return SimpleNamespace(
            id=source.id,
            project_id=source.project_id,
            claim_token=source.claim_token,
        )


def project_reserve(environment, source, **changes):
    values = {
        "project_id": source.project_id,
        "job_id": None,
        "project_linkage_review_id": source.id,
        "project_linkage_claim_token": source.claim_token,
        "call_id": uuid4(),
        "reviewer": "project_summary",
        "instruction_version": "fixed-project-review-v1",
        "model": "fixed-test-model",
        "endpoint": "https://provider.invalid/v1/chat/completions",
        "input_hash": "b" * 64,
        "reserved_tokens": 1000,
        "prices": PRICES,
    }
    values.update(changes)
    return reserve_call(environment.sessions, **values)


def test_project_client_forwards_trusted_source_before_http_and_settles_once(monkeypatch):
    ledger = Ledger()
    monkeypatch.setattr(llm, "_usage_service", lambda: ledger)
    source, token = uuid4(), uuid4()
    seen = []

    def provider(request):
        assert len(ledger.reserves) == 1 and ledger.settlements == []
        reservation = ledger.reserves[0]
        assert reservation["job_id"] is None
        assert reservation["project_linkage_review_id"] == source
        assert reservation["project_linkage_claim_token"] == token
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=response_body())

    client = LLMClient(
        None,
        transport=httpx2.MockTransport(provider),
        secrets={"DEEPSEEK_API_KEY": FAKE_KEY},
    )
    result = client.complete_json(
        project_id=uuid4(),
        job_id=None,
        project_linkage_review_id=source,
        project_linkage_claim_token=token,
        reviewer="project_summary",
        instruction_version="fixed-project-review-v1",
        messages=MESSAGES,
    )
    assert result == {"findings": []} and len(seen) == 1
    assert seen[0]["model"] == "deepseek-flash"
    assert ledger.settlements[0]["usage"] == USAGE


@pytest.mark.parametrize(
    "changes",
    [
        {"job_id": uuid4()},
        {"spec_draft_request_id": uuid4()},
        {"project_linkage_review_id": None},
        {"project_linkage_review_id": "invalid"},
        {"project_linkage_claim_token": None},
        {"project_linkage_claim_token": "invalid"},
        {"job_id": uuid4(), "project_linkage_review_id": None},
    ],
)
def test_project_client_rejects_ambiguous_source_or_missing_fence_before_reservation(
    changes, monkeypatch
):
    ledger = Ledger()
    monkeypatch.setattr(llm, "_usage_service", lambda: ledger)
    client = LLMClient(
        None,
        transport=httpx2.MockTransport(lambda _: pytest.fail("Invalid source sent HTTP")),
        secrets={"DEEPSEEK_API_KEY": FAKE_KEY},
    )
    values = {
        "project_id": uuid4(),
        "job_id": None,
        "project_linkage_review_id": uuid4(),
        "project_linkage_claim_token": uuid4(),
        "reviewer": "project_summary",
        "instruction_version": "fixed-project-review-v1",
        "messages": MESSAGES,
    }
    values.update(changes)
    with pytest.raises(LLMRequestError) as error:
        client.complete_json(**values)
    assert error.value.code == "request_invalid"
    assert ledger.reserves == ledger.settlements == []


def test_project_summary_and_comparison_share_one_review_admission_and_export_usage(
    auth_environment,
):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    calls = []
    for reviewer in ("project_summary", "project_summary", "project_linkage"):
        identity = project_reserve(auth_environment, source, reviewer=reviewer)
        assert settle_call(
            auth_environment.sessions, identity, usage=KNOWN_USAGE, status="succeeded"
        )
        calls.append(identity)
    with auth_environment.sessions() as db:
        admissions = list(db.scalars(select(ReviewAdmission)))
        assert len(admissions) == 1
        admission = admissions[0]
        assert admission.job_id is None and admission.project_linkage_review_id == source.id
        assert admission.call_count == 3
        buckets = list(db.scalars(select(DailyBudget)))
        assert len(buckets) == 2
        assert all(bucket.review_count == 1 and bucket.charged_tokens == 450 for bucket in buckets)
        events = list(db.scalars(select(Event).where(Event.object_id == str(source.id))))
        reserved = [event for event in events if event.event_type == "llm_call_reserved"]
        settled = [event for event in events if event.event_type == "llm_call_settled"]
        assert len(reserved) == len(settled) == 3
        assert all(event.plan_version_id == UUID(team.plan["id"]) for event in events)
        assert all(event.spec_version_id == UUID(team.spec["id"]) for event in events)
        assert all(event.details["initiated_by"] == team.leader_id for event in events)
        assert all(event.object_type == "project_linkage_review" for event in events)
        settings = Settings(_env_file=None, platform_admin_user_ids=team.leader_id)
        today = datetime.now(UTC).date()
        report = admin_usage.usage_report(
            db, UUID(team.leader_id), settings, today, today, source.project_id
        )
        assert report.totals.calls == 3 and report.totals.total_tokens == 450
        assert {item.reviewer for item in report.by_reviewer} == {
            "project_summary",
            "project_linkage",
        }
        rows = list(
            csv.DictReader(
                io.StringIO(
                    admin_usage.usage_csv(
                        db, UUID(team.leader_id), settings, today, today, source.project_id
                    ).lstrip("\ufeff")
                )
            )
        )
        assert {row["call_id"] for row in rows} == {str(identity) for identity in calls}
        assert sum(int(row["total_tokens"]) for row in rows) == 450


@pytest.mark.parametrize("mutation", ["stale_token", "expired_lease", "queued", "other_project"])
def test_project_source_must_match_active_project_lease_before_any_quota(
    auth_environment, mutation
):
    from app.models.project_linkage import ProjectLinkageReview

    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    changes = {}
    if mutation == "stale_token":
        changes["project_linkage_claim_token"] = uuid4()
    elif mutation == "other_project":
        changes["project_id"] = uuid4()
    else:
        with auth_environment.sessions() as db:
            row = db.get(ProjectLinkageReview, source.id)
            if mutation == "expired_lease":
                row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
            else:
                row.status, row.claim_token, row.lease_until = "queued", None, None
            db.commit()
    before = account_snapshot(auth_environment)
    with pytest.raises(LLMUsageError):
        project_reserve(auth_environment, source, **changes)
    assert account_snapshot(auth_environment) == before


def test_project_source_rechecks_current_leader_before_budget(auth_environment):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    with auth_environment.sessions() as db:
        db.execute(
            update(ProjectMember)
            .where(
                ProjectMember.project_id == source.project_id,
                ProjectMember.user_id == UUID(team.leader_id),
            )
            .values(role="member")
        )
        db.commit()
    before = account_snapshot(auth_environment)
    with pytest.raises(LeaderRequiredError):
        project_reserve(auth_environment, source)
    assert account_snapshot(auth_environment) == before


def test_project_source_requires_current_data_transfer_consent(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    monkeypatch.setattr(model_disclosure, "DISCLOSURE_VERSION", "test-unconsented-new-notice")
    before = account_snapshot(auth_environment)
    with pytest.raises(model_disclosure.ModelConsentRequiredError):
        project_reserve(auth_environment, source)
    assert account_snapshot(auth_environment) == before


def test_project_provider_observes_committed_reservation_without_holding_source_lock(
    auth_environment,
):
    from app.models.project_linkage import ProjectLinkageReview

    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    identity = uuid4()

    def provider(request):
        with auth_environment.sessions() as db:
            call = db.get(ModelCall, identity)
            assert call.status == "reserved"
            assert db.get(ReviewAdmission, call.admission_id).project_linkage_review_id == source.id
            # An exclusive NOWAIT row lock succeeds while the substitute provider is running.
            # The reservation transaction therefore released all queue/source locks before HTTP.
            row = db.scalar(
                select(ProjectLinkageReview)
                .where(ProjectLinkageReview.id == source.id)
                .with_for_update(nowait=True)
            )
            assert row.status == "running"
        return httpx2.Response(200, json=response_body())

    client = LLMClient(
        auth_environment.sessions,
        secrets={"DEEPSEEK_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(provider),
    )
    assert client.complete_json(
        project_id=source.project_id,
        job_id=None,
        project_linkage_review_id=source.id,
        project_linkage_claim_token=source.claim_token,
        call_id=identity,
        reviewer="project_summary",
        instruction_version="fixed-project-review-v1",
        messages=MESSAGES,
    ) == {"findings": []}
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, identity)
        assert call.status == "succeeded" and call.total_tokens == 40


@pytest.mark.parametrize("reviewer", ["logic", "linkage", "spec_draft", "spec_compliance"])
def test_project_source_cannot_authorize_delivery_or_draft_reviewers(auth_environment, reviewer):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    before = account_snapshot(auth_environment)
    with pytest.raises(LLMUsageError):
        project_reserve(auth_environment, source, reviewer=reviewer)
    assert account_snapshot(auth_environment) == before


def test_project_finite_calls_and_duplicate_ids_do_not_reserve_twice(auth_environment):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    limits = QuotaLimits(max_calls_per_review=2)
    first = project_reserve(auth_environment, source, limits=limits)
    project_reserve(auth_environment, source, reviewer="project_linkage", limits=limits)
    before = account_snapshot(auth_environment)
    with pytest.raises(LLMDuplicateCallError):
        project_reserve(auth_environment, source, call_id=first, limits=limits)
    with pytest.raises(LLMCallLimitError):
        project_reserve(auth_environment, source, limits=limits)
    assert account_snapshot(auth_environment) == before


def test_project_zero_quota_pauses_before_provider_and_no_call_is_created(auth_environment):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    with auth_environment.sessions() as db:
        db.add(LLMProjectPolicy(project_id=source.project_id, daily_review_limit=0))
        db.commit()
    before = account_snapshot(auth_environment)
    with pytest.raises(QuotaExceededError) as error:
        project_reserve(auth_environment, source)
    assert error.value.scope == "project_reviews"
    assert account_snapshot(auth_environment) == before


def test_project_lease_expiry_after_reserve_does_not_discard_real_usage(auth_environment):
    from app.models.project_linkage import ProjectLinkageReview

    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    identity = project_reserve(auth_environment, source)
    with auth_environment.sessions() as db:
        row = db.get(ProjectLinkageReview, source.id)
        row.claim_token, row.lease_until = uuid4(), datetime.now(UTC) + timedelta(minutes=10)
        db.commit()
    assert settle_call(auth_environment.sessions, identity, usage=KNOWN_USAGE, status="succeeded")
    with auth_environment.sessions() as db:
        call = db.get(ModelCall, identity)
        assert call.total_tokens == 150 and call.status == "succeeded"
        assert db.get(ReviewAdmission, call.admission_id).project_linkage_review_id == source.id


def test_database_admission_requires_exactly_one_delivery_or_project_source(auth_environment):
    team = ready_team(auth_environment)
    source = project_source(auth_environment, team)
    job = formal_job(auth_environment, team)
    identity = reserve(auth_environment, job)
    with auth_environment.sessions() as db:
        admission_id = db.get(ModelCall, identity).admission_id
        with pytest.raises(DBAPIError) as invalid:
            db.execute(
                update(ReviewAdmission)
                .where(ReviewAdmission.id == admission_id)
                .values(
                    project_linkage_review_id=source.id,
                )
            )
        assert invalid.value.orig.sqlstate == "23514"
        db.rollback()
        with pytest.raises(DBAPIError) as missing:
            db.execute(
                update(ReviewAdmission)
                .where(ReviewAdmission.id == admission_id)
                .values(
                    job_id=None,
                )
            )
        assert missing.value.orig.sqlstate == "23514"
