"""完整衔接审核的人类HTTP及MCP边界，自动化不进行任何真实外发。"""

import asyncio
import json
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_deliveries import ready_team, upload_delivery
from test_final_review import decide
from test_llm_transport import response_body
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client
from test_model_disclosure import unconsented_team
from test_project_linkage_pipeline import opinion, summary_outputs
from test_review_reports import process

from app.models import Event
from app.models.llm_usage import DailyBudget, ModelCall
from app.models.project_linkage import ProjectLinkageReview
from app.review.llm import LLMClient
from app.review.project_linkage import execute_project_linkage
from app.services import project_linkage
from app.services.auth import COOKIE_NAME


def path(team, review_id=None):
    base = f"/api/projects/{team.project['id']}/linkage-reviews"
    return f"{base}/{review_id}" if review_id else base


def start(team, client=None, headers=None):
    return (client or team.leader).post(
        path(team), headers=CSRF_HEADERS if headers is None else headers
    )


def test_http_enqueues_detached_report_and_members_can_read_frozen_version_metadata(
    auth_environment,
):
    team = ready_team(auth_environment)
    response = start(team)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["status"] == "queued" and value["result"] is None
    assert value["plan_version_id"] == team.plan["id"]
    assert value["spec_version_id"] == team.spec["id"]
    assert value["requested_by"] == team.leader_id
    assert not value["can_retry"] and not value["can_convert"]
    assert not {"frozen_input", "claim_token", "lease_until"} & value.keys()
    for client in (team.member, team.other_member):
        detail = client.get(path(team, value["id"]))
        assert detail.status_code == 200 and detail.json() == value
        assert detail.headers["cache-control"] == "no-store"
        assert client.get(path(team)).json() == [value]
    assert start(team).status_code == 409
    with auth_environment.sessions() as db:
        rows = list(db.scalars(select(ProjectLinkageReview)))
        assert len(rows) == 1 and rows[0].attempt == 0
        event = db.scalar(select(Event).where(Event.event_type == "project_linkage_requested"))
        assert event.actor_type == "human" and event.actor_id == team.leader_id
        assert event.plan_version_id == UUID(team.plan["id"])
        assert event.details["selection"] == "latest_adopted"


def test_members_and_admin_do_not_gain_leader_actions_and_outsiders_cannot_read(auth_environment):
    team = ready_team(auth_environment)
    review_id = start(team).json()["id"]
    admin = auth_environment.make_client(platform_admin_user_ids=team.member_id)
    assert login(admin, "member").json()["is_admin"]
    for client in (team.member, team.other_member, admin):
        assert start(team, client).status_code == 403
        assert (
            client.post(path(team, review_id) + "/retry", headers=CSRF_HEADERS).status_code == 403
        )
        assert (
            client.post(
                path(team, review_id) + f"/findings/{uuid4()}/request",
                json={"title": "任务", "content": "补充说明", "target_user_id": team.member_id},
                headers=CSRF_HEADERS,
            ).status_code
            == 403
        )
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    for url in (path(team), path(team, review_id)):
        assert outsider.get(url).status_code == 404
        assert auth_environment.make_client().get(url).status_code == 401
    assert start(team, outsider).status_code == 404
    foreign = path(team, review_id).replace(team.project["id"], str(uuid4()))
    assert team.leader.get(foreign).status_code == 404
    assert team.leader.get(path(team, str(uuid4()))).status_code == 404


@pytest.mark.parametrize(
    "headers", [{}, {"X-CSRF-Protection": "0"}, {**CSRF_HEADERS, "Origin": "https://bad.invalid"}]
)
def test_start_retry_and_convert_require_csrf(auth_environment, headers):
    team = ready_team(auth_environment)
    review_id = start(team).json()["id"]
    assert start(team, headers=headers).status_code == 403
    assert team.leader.post(path(team, review_id) + "/retry", headers=headers).status_code == 403
    assert (
        team.leader.post(
            path(team, review_id) + f"/findings/{uuid4()}/request",
            json={"title": "任务", "content": "补充说明", "target_user_id": team.member_id},
            headers=headers,
        ).status_code
        == 403
    )


def test_unconsented_project_is_not_enqueued(auth_environment):
    team = unconsented_team(auth_environment)
    response = start(team)
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ProjectLinkageReview))) == []


def test_http_to_fixed_model_report_and_request_response_resolution(auth_environment):
    """公开触发→持久任务→真实接入层的固定响应→分派→回应→发起人解决。"""
    from app.storage import LocalStorage

    team = ready_team(auth_environment)
    for index, client in enumerate((team.member, team.other_member)):
        response = upload_delivery(
            client,
            team,
            f"模块{index}样本量为{120 + index * 30}人。".encode(),
            module_id=team.modules[index]["id"],
        )
        assert response.status_code == 201
        delivery = response.json()
        process(auth_environment, team, delivery)
        assert decide(auth_environment, team, delivery)["outcome"] == "adopted"
    review_id = start(team).json()["id"]
    outputs = [*summary_outputs(), {"findings": [opinion()], "coverage_notes": []}]
    requests = []

    def fixed_provider(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=response_body(content=json.dumps(outputs.pop(0))))

    model = LLMClient(
        auth_environment.sessions,
        secrets={"DEEPSEEK_API_KEY": "fixed-m4-key"},
        transport=httpx2.MockTransport(fixed_provider),
    )
    with auth_environment.sessions() as db:
        claim = project_linkage.claim_next(db)
        db.commit()
    assert str(claim.review_id) == review_id
    reference = execute_project_linkage(
        auth_environment.sessions,
        claim,
        storage=LocalStorage(team.leader.app.state.settings.storage_dir),
        client=model,
    )
    with auth_environment.sessions() as db:
        assert project_linkage.complete_job(db, claim, reference)
        db.commit()
    report = team.leader.get(path(team, review_id)).json()
    assert report["status"] == report["result"]["coverage"] == "completed"
    assert len(requests) == 3 and outputs == []
    assert report["can_convert"]
    finding = report["result"]["findings"][0]
    assert finding["evidence_status"] == "verified" and len(finding["module_ids"]) == 2
    endpoint = path(team, review_id) + f"/findings/{finding['id']}/request"
    body = {
        "title": "统一模块样本口径",
        "content": "请核对数据并回应。",
        "target_user_id": team.member_id,
    }
    response = team.leader.post(endpoint, json=body, headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    request_info = response.json()
    assert review_id in request_info["content"] and finding["id"] in request_info["content"]
    repeated = team.leader.post(endpoint, json=body, headers=CSRF_HEADERS)
    assert repeated.json()["id"] == request_info["id"]
    refreshed = team.member.get(path(team, review_id)).json()
    assert refreshed["result"]["findings"][0]["request_id"] == request_info["id"]
    request_url = f"/api/projects/{team.project['id']}/requests/{request_info['id']}"
    assert team.member.post(request_url + "/accept", headers=CSRF_HEADERS).status_code == 200
    assert (
        team.member.post(
            request_url + "/respond", json={"content": "已核对统一为120人。"}, headers=CSRF_HEADERS
        ).status_code
        == 200
    )
    assert team.member.post(request_url + "/resolve", headers=CSRF_HEADERS).status_code == 403
    solved = team.leader.post(request_url + "/resolve", headers=CSRF_HEADERS)
    assert solved.status_code == 200 and solved.json()["status"] == "resolved"
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall).order_by(ModelCall.created_at, ModelCall.id)))
        assert len(calls) == 3 and all(call.total_tokens == 40 for call in calls)
        assert {call.reviewer for call in calls} == {"project_summary", "project_linkage"}
        assert finding["input_hash"] == calls[-1].input_hash
        assert all(bucket.review_count == 1 for bucket in db.scalars(select(DailyBudget)))


@pytest.mark.parametrize("cookie", [False, True])
def test_agent_token_cannot_read_trigger_retry_or_convert_with_leader_cookie(
    auth_environment, cookie
):
    team = ready_team(auth_environment)
    review_id = start(team).json()["id"]
    agent = register_agent(team.leader, team.project["id"], "负责人助手")
    client = team.leader if cookie else auth_environment.make_client()
    headers = {**CSRF_HEADERS, **bearer(agent["token"])}
    requests = [
        ("GET", path(team), None),
        ("GET", path(team, review_id), None),
        ("POST", path(team), None),
        ("POST", path(team, review_id) + "/retry", None),
        (
            "POST",
            path(team, review_id) + f"/findings/{uuid4()}/request",
            {"title": "任务", "content": "补充说明", "target_user_id": team.member_id},
        ),
    ]
    for method, url, body in requests:
        response = client.request(method, url, headers=headers, json=body)
        assert response.status_code == 403, response.text
        assert response.headers["cache-control"] == "no-store"
    with auth_environment.sessions() as db:
        events = list(
            db.scalars(select(Event).where(Event.event_type == "agent_permission_denied"))
        )
        assert len(events) == 5
        assert all(item.actor_id == agent["agent"]["id"] for item in events)
        assert len(list(db.scalars(select(ProjectLinkageReview)))) == 1


@pytest.mark.parametrize("cookie", [False, True])
def test_mcp_does_not_expose_project_review_to_leader_owned_agent(
    auth_environment, mcp_network, cookie
):
    team = ready_team(auth_environment)
    agent = register_agent(team.leader, team.project["id"], "负责人助手")
    headers = {"Cookie": f"{COOKIE_NAME}={team.leader.cookies.get(COOKIE_NAME)}"} if cookie else {}

    async def scenario():
        async with official_client(mcp_network, agent["token"], headers=headers) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert (
                not {"project_linkage_review", "start_linkage_review", "convert_linkage_finding"}
                & tools
            )
            result = await client.call_tool(
                "project_linkage_review", {"project_id": team.project["id"]}
            )
            assert result.is_error

    asyncio.run(scenario())
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ProjectLinkageReview))) == []
