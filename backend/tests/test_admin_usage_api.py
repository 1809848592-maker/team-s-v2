"""M3-13 管理员跨项目账本及 CSV 的人类 HTTP 权限与输入回归。"""

import csv
import io
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_admin_usage import dataset
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_deliveries import ready_team
from test_llm_usage import quota_clock as quota_clock

from app.models import Event
from app.models.llm_usage import LLMProjectPolicy
from app.services import admin_usage

PARAMS = {"start": "2026-11-11", "end": "2026-11-12"}


def admin_client(environment, team, *, outsider=False):
    if outsider:
        person = environment.make_client()
        identity = register(person, "platform-finance").json()["id"]
        username = "platform-finance"
    else:
        identity, username = team.leader_id, "leader"
    client = environment.make_client(platform_admin_user_ids=identity)
    assert login(client, username).json()["is_admin"]
    return client, identity


def test_admin_outside_project_sees_only_usage_metadata_and_csv_totals(
    auth_environment,
    quota_clock,
):
    team, other, _ = dataset(auth_environment, quota_clock)
    admin, _ = admin_client(auth_environment, team, outsider=True)
    response = admin.get("/api/admin/usage", params=PARAMS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["totals"]["calls"] == 6 and data["totals"]["total_tokens"] == 465
    assert data["totals"]["estimated_cost_cny"] == "0.0015648000"
    assert data["totals"]["unknown_usage_calls"] == 2
    assert {item["project_id"] for item in data["by_project"]} == {
        team.project["id"],
        other.project["id"],
    }
    assert admin.get(f"/api/projects/{team.project['id']}").status_code == 404
    export = admin.get("/api/admin/usage.csv", params=PARAMS)
    assert export.status_code == 200 and export.headers["cache-control"] == "no-store"
    assert export.headers["content-type"].startswith("text/csv")
    assert (
        'filename="teams-usage-2026-11-11-2026-11-12.csv"' in export.headers["content-disposition"]
    )
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
    assert len(rows) == 6
    assert (
        sum(int(row["total_tokens"]) for row in rows if row["total_tokens"])
        == data["totals"]["total_tokens"]
    )
    assert "provider.invalid" not in export.text and "api_key" not in response.text
    filtered = admin.get("/api/admin/usage", params={**PARAMS, "project_id": other.project["id"]})
    assert filtered.status_code == 200 and filtered.json()["totals"]["total_tokens"] == 165


@pytest.mark.parametrize(
    "endpoint", ["/api/admin/usage", "/api/admin/usage.csv", "/api/admin/projects"]
)
def test_members_and_leaders_are_not_admin_and_anonymous_cannot_read(auth_environment, endpoint):
    team = ready_team(auth_environment)
    for client in (team.leader, team.member, team.other_member):
        response = client.get(endpoint, params=PARAMS)
        assert response.status_code == 403, response.text
        assert response.headers["cache-control"] == "no-store"
    assert auth_environment.make_client().get(endpoint, params=PARAMS).status_code == 401


def test_admin_lists_effective_project_quotas_and_updates_with_csrf(auth_environment):
    team = ready_team(auth_environment)
    admin, identity = admin_client(auth_environment, team, outsider=True)
    response = admin.get("/api/admin/projects")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == [
        {"id": team.project["id"], "name": team.project["name"], "daily_review_limit": 20}
    ]
    path = f"/api/admin/projects/{team.project['id']}/quota"
    assert admin.put(path, json={"daily_review_limit": 0}).status_code == 403
    assert (
        admin.put(
            path,
            json={"daily_review_limit": 0},
            headers={**CSRF_HEADERS, "Origin": "https://foreign.invalid"},
        ).status_code
        == 403
    )
    response = admin.put(path, json={"daily_review_limit": 0}, headers=CSRF_HEADERS)
    assert response.status_code == 200 and response.json()["daily_review_limit"] == 0
    assert response.headers["cache-control"] == "no-store"
    assert admin.put(path, json={"daily_review_limit": 0}, headers=CSRF_HEADERS).status_code == 200
    assert (
        team.leader.put(path, json={"daily_review_limit": 9}, headers=CSRF_HEADERS).status_code
        == 403
    )
    assert (
        auth_environment.make_client()
        .put(path, json={"daily_review_limit": 9}, headers=CSRF_HEADERS)
        .status_code
        == 401
    )
    with auth_environment.sessions() as db:
        events = list(
            db.scalars(select(Event).where(Event.event_type == "llm_project_quota_updated"))
        )
        assert len(events) == 1 and events[0].actor_id == identity


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"daily_review_limit": -1},
        {"daily_review_limit": 2_147_483_648},
        {"daily_review_limit": True},
        {"daily_review_limit": "10"},
        {"daily_review_limit": 1.5},
        {"daily_review_limit": 3, "api_key": "never-accept-this"},
    ],
)
def test_invalid_quota_requests_never_create_policy_or_echo_sensitive_input(auth_environment, body):
    team = ready_team(auth_environment)
    admin, _ = admin_client(auth_environment, team)
    response = admin.put(
        f"/api/admin/projects/{team.project['id']}/quota", json=body, headers=CSRF_HEADERS
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "never-accept-this" not in response.text
    with auth_environment.sessions() as db:
        assert db.get(LLMProjectPolicy, UUID(team.project["id"])) is None


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"start": "bad", "end": "2026-11-12"},
        {"start": "2026-11-12", "end": "2026-11-11"},
        {"start": "2025-01-01", "end": "2026-01-02"},
        {**PARAMS, "project_id": "bad"},
    ],
)
def test_invalid_dates_or_ids_fail_both_report_formats(auth_environment, params):
    team = ready_team(auth_environment)
    admin, _ = admin_client(auth_environment, team)
    for path in ("/api/admin/usage", "/api/admin/usage.csv"):
        response = admin.get(path, params=params)
        assert response.status_code == 422, response.text
        assert response.headers["cache-control"] == "no-store"


def test_missing_project_and_oversized_report_have_explicit_errors(
    auth_environment, quota_clock, monkeypatch
):
    team, _, _ = dataset(auth_environment, quota_clock)
    admin, _ = admin_client(auth_environment, team)
    for path in ("/api/admin/usage", "/api/admin/usage.csv"):
        assert admin.get(path, params={**PARAMS, "project_id": str(uuid4())}).status_code == 404
    assert (
        admin.put(
            f"/api/admin/projects/{uuid4()}/quota",
            json={"daily_review_limit": 0},
            headers=CSRF_HEADERS,
        ).status_code
        == 404
    )
    monkeypatch.setattr(admin_usage, "MAX_REPORT_CALLS", 1)
    for path in ("/api/admin/usage", "/api/admin/usage.csv"):
        response = admin.get(path, params=PARAMS)
        assert response.status_code == 422 and "缩小" in response.json()["detail"]


@pytest.mark.parametrize("with_cookie", [False, True])
def test_admin_owned_agent_cannot_read_global_usage_or_change_quota(auth_environment, with_cookie):
    team = ready_team(auth_environment)
    registered = register_agent(team.leader, team.project["id"], "管理员的助手")
    admin, _ = admin_client(auth_environment, team)
    client = (
        admin
        if with_cookie
        else auth_environment.make_client(platform_admin_user_ids=team.leader_id)
    )
    headers = {**CSRF_HEADERS, **bearer(registered["token"])}
    for path in ("/api/admin/usage", "/api/admin/usage.csv", "/api/admin/projects"):
        assert client.get(path, params=PARAMS, headers=headers).status_code == 403
    assert (
        client.put(
            f"/api/admin/projects/{team.project['id']}/quota",
            json={"daily_review_limit": 0},
            headers=headers,
        ).status_code
        == 403
    )
    with auth_environment.sessions() as db:
        denied = list(
            db.scalars(select(Event).where(Event.event_type == "agent_permission_denied"))
        )
        assert len(denied) == 4
        assert {event.actor_id for event in denied} == {registered["agent"]["id"]}
        assert db.get(LLMProjectPolicy, UUID(team.project["id"])) is None
