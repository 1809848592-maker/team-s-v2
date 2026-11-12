"""M3-15 人类负责人HTTP与真实MCP通道边界；模型均为固定响应替身。"""

import asyncio
from copy import deepcopy
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, wait_for_records
from test_model_disclosure import unconsented_team
from test_modules import create_team
from test_spec_drafting import (
    DRAFT_OUTPUT,
    PRIVATE_KEY,
    count,
    fixed_model,
    request_body,
    source_model,
)
from test_specs import publish_spec

from app.models import Event, Project, SpecVersion
from app.models.llm_usage import DailyBudget, ModelCall
from app.services.auth import COOKIE_NAME


def path(team, project_id=None):
    return f"/api/projects/{project_id or team.project['id']}/spec/draft"


def install_model(monkeypatch, environment, **options):
    from app.api import spec_drafting

    client, attempts = fixed_model(environment, **options)
    monkeypatch.setattr(spec_drafting, "build_llm_client", lambda *_args, **_kwargs: client)
    return attempts


def submit(team, body=None, *, client=None, headers=None, project_id=None):
    return (client or team.leader).post(
        path(team, project_id),
        json=request_body() if body is None else body,
        headers=CSRF_HEADERS if headers is None else headers,
    )


def test_http_generation_returns_editable_items_and_only_human_publish_creates_version(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    body = request_body()
    response = submit(team, body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"items", "request_id", "call_id", "instruction_version"}
    assert result["items"] == DRAFT_OUTPUT["items"]
    assert result["request_id"] == result["call_id"] == body["request_id"]
    assert len(attempts) == 1 and count(auth_environment, SpecVersion) == 0
    changed = deepcopy(result["items"])
    changed[0]["requirement"] = "负责人核对后修改的规范要求。"
    published = team.leader.post(
        f"/api/projects/{team.project['id']}/spec/versions",
        json={"change_note": "人工确认并修改辅助草稿", "items": changed},
        headers=CSRF_HEADERS,
    )
    assert published.status_code == 201
    assert published.json()["items"] == changed and count(auth_environment, SpecVersion) == 1
    assert len(attempts) == 1


def test_http_release_auth_dependency_transaction_before_model_connection(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    engine = auth_environment.sessions.kw["bind"]
    observed = []

    def check(_request):
        observed.append(engine.pool.checkedout())
        assert observed[-1] == 0
        with auth_environment.sessions() as db:
            assert db.scalar(
                select(Project.id)
                .where(Project.id == UUID(team.project["id"]))
                .with_for_update(nowait=True)
            )

    attempts = install_model(monkeypatch, auth_environment, effect=check)
    response = submit(team)
    assert response.status_code == 200, response.text
    assert observed == [0] and len(attempts) == 1


def test_only_current_project_leader_human_can_generate_not_admin_members_or_outsiders(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    admin = auth_environment.make_client(platform_admin_user_ids=team.member_id)
    assert login(admin, "member").json()["is_admin"]
    for client in (team.member, team.other_member, admin):
        assert submit(team, client=client).status_code == 403
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    assert submit(team, client=outsider).status_code == 404
    assert submit(team, project_id=str(uuid4())).status_code == 404
    assert submit(team, client=auth_environment.make_client()).status_code == 401
    assert (
        attempts == []
        and count(auth_environment, source_model()) == count(auth_environment, ModelCall) == 0
    )


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-CSRF-Protection": "0"}, {**CSRF_HEADERS, "Origin": "https://attacker.invalid"}],
)
def test_http_requires_csrf_and_same_origin(auth_environment, monkeypatch, headers):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    assert submit(team, headers=headers).status_code == 403
    assert attempts == [] and count(auth_environment, source_model()) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"description": ""},
        {"description": " \n\t "},
        {"description": "x" * 6001},
        {"description": True},
        {"description": ["要求"]},
        {"description": "bad\x00text"},
        {"description": "bad\x01text"},
        {"request_id": "not-a-uuid"},
        {"request_id": None},
        {"disclosure_version": ""},
        {"disclosure_version": "x" * 81},
        {"model": "injected-model"},
        {"api_key": "chosen-key-never-echo"},
        {"published": True},
    ],
)
def test_invalid_inputs_never_call_model_and_do_not_echo_sensitive_values(
    auth_environment, monkeypatch, changes
):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    response = submit(team, request_body(**changes))
    assert response.status_code == 422, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "chosen-key-never-echo" not in response.text
    assert all("input" not in value and "ctx" not in value for value in response.json()["detail"])
    assert attempts == [] and count(auth_environment, source_model()) == 0


@pytest.mark.parametrize("field", ["description", "request_id", "disclosure_version"])
def test_required_request_fields_cannot_be_omitted(auth_environment, monkeypatch, field):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    body = request_body()
    del body[field]
    assert submit(team, body).status_code == 422
    assert attempts == []


def test_no_consent_and_old_notice_block_before_budget_or_model(auth_environment, monkeypatch):
    team = unconsented_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    assert submit(team).status_code == 409
    assert attempts == []
    for model in (source_model(), ModelCall, DailyBudget):
        assert count(auth_environment, model) == 0
    from app.services.model_disclosure import DISCLOSURE_VERSION

    accepted = team.leader.post(
        f"/api/projects/{team.project['id']}/model-consent",
        json={"disclosure_version": DISCLOSURE_VERSION},
        headers=CSRF_HEADERS,
    )
    assert accepted.status_code == 200
    assert submit(team, request_body(disclosure_version="old-notice-version")).status_code == 409
    assert attempts == [] and count(auth_environment, source_model()) == 0


@pytest.mark.parametrize(
    "output",
    [
        {"items": []},
        {"items": [{**DRAFT_OUTPUT["items"][0], "category": "injected"}]},
        {**DRAFT_OUTPUT, "published": True},
        "not-a-json-object",
    ],
)
def test_invalid_model_output_is_one_call_error_and_preserves_published_spec_and_actual_usage(
    auth_environment, monkeypatch, output
):
    team = create_team(auth_environment)
    original = publish_spec(team.leader, team.project["id"])
    attempts = install_model(monkeypatch, auth_environment, output=output)
    response = submit(team)
    assert response.status_code == 502, response.text
    assert "重试" in response.json()["detail"]
    assert len(attempts) == count(auth_environment, ModelCall) == 1
    assert count(auth_environment, SpecVersion) == 1
    assert team.leader.get(f"/api/projects/{team.project['id']}/spec").json() == original
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        assert call.total_tokens == 150 and call.estimated_cost_cny is not None
        assert not list(db.scalars(select(Event).where(Event.event_type == "spec_draft_generated")))


def test_http_duplicate_request_returns_conflict_without_second_external_call(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    body = request_body()
    assert submit(team, body).status_code == 200
    for changed in (body, {**body, "description": "不同要求也不能复用已经外发的请求号。"}):
        response = submit(team, changed)
        assert response.status_code == 409, response.text
    assert len(attempts) == count(auth_environment, ModelCall) == 1


def test_disabled_real_model_requires_explicit_switch_even_if_key_configured(auth_environment):
    team = create_team(auth_environment)
    client = auth_environment.make_client(deepseek_api_key=PRIVATE_KEY, llm_enable_live_calls=False)
    assert login(client, "leader").status_code == 200
    response = submit(team, client=client)
    assert response.status_code == 503, response.text
    assert PRIVATE_KEY not in response.text
    assert count(auth_environment, ModelCall) == count(auth_environment, DailyBudget) == 0


def test_project_quota_zero_returns_retryable_429_without_model_request(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    from app.config import Settings

    settings = Settings(_env_file=None, deepseek_api_key=PRIVATE_KEY, llm_project_daily_reviews=0)
    attempts = install_model(monkeypatch, auth_environment, settings=settings)
    response = submit(team)
    assert response.status_code == 429, response.text
    assert int(response.headers["retry-after"]) >= 1
    assert "retry_at" in response.json()
    assert attempts == [] and count(auth_environment, ModelCall) == 0


@pytest.mark.parametrize("owner", ["leader", "member"])
@pytest.mark.parametrize("cookie", [False, True])
def test_http_agent_cannot_generate_even_with_leader_owner_cookie_and_is_audited(
    auth_environment, monkeypatch, owner, cookie
):
    team = create_team(auth_environment)
    attempts = install_model(monkeypatch, auth_environment)
    person = getattr(team, owner)
    created = register_agent(person, team.project["id"])
    response = submit(
        team,
        client=person if cookie else auth_environment.make_client(),
        headers={**CSRF_HEADERS, **bearer(created["token"])},
    )
    assert response.status_code == 403
    assert attempts == [] and count(auth_environment, source_model()) == 0
    with auth_environment.sessions() as db:
        event = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert event.actor_id == created["agent"]["id"]
        assert event.details["action"] == "generate_spec_draft"


@pytest.mark.parametrize("cookie", [False, True])
def test_real_mcp_never_exposes_drafting_or_publishing_to_leader_owned_agent(
    auth_environment, mcp_network, cookie
):
    team = create_team(auth_environment)
    created = register_agent(team.leader, team.project["id"], "负责人起草助手")
    headers = {"Cookie": f"{COOKIE_NAME}={team.leader.cookies.get(COOKIE_NAME)}"} if cookie else {}

    async def scenario():
        async with official_client(mcp_network, created["token"], headers=headers) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "generate_spec_draft" not in names and "publish_spec" not in names
            result = await client.call_tool(
                "generate_spec_draft",
                {
                    "project_id": team.project["id"],
                    **request_body(),
                },
            )
            assert result.is_error

    asyncio.run(scenario())
    records = wait_for_records(auth_environment, team, "agent_tool_call", 1)
    assert records[0].details == {"tool": "unknown", "outcome": "error"}
    with auth_environment.sessions() as db:
        event = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert (
            event.actor_id == created["agent"]["id"]
            and event.details["action"] == "mcp_unknown_tool"
        )
    assert (
        count(auth_environment, source_model())
        == count(auth_environment, SpecVersion)
        == count(auth_environment, ModelCall)
        == 0
    )


@pytest.mark.parametrize("failure", ["timeout", "invalid_usage"])
def test_transport_failure_or_unknown_usage_returns_no_draft_without_automatic_retry(
    auth_environment, monkeypatch, failure
):
    team = create_team(auth_environment)

    def timeout(request):
        if failure == "timeout":
            raise httpx2.ReadTimeout("fixed mock timeout", request=request)

    attempts = install_model(
        monkeypatch,
        auth_environment,
        effect=timeout,
        usage={} if failure == "invalid_usage" else None,
    )
    response = submit(team)
    assert response.status_code == 502, response.text
    assert len(attempts) == count(auth_environment, ModelCall) == 1
    assert count(auth_environment, SpecVersion) == 0
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        assert call.status == "unknown" and call.total_tokens is None
        assert call.estimated_cost_cny is None
        assert call.reserved_tokens > 0
        assert all(
            row.reserved_tokens == call.reserved_tokens for row in db.scalars(select(DailyBudget))
        )
        assert not list(db.scalars(select(Event).where(Event.event_type == "spec_draft_generated")))
