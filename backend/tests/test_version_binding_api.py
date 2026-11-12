"""M3-12 版本标记及负责人要求重交的真实 HTTP/PG 权限回归。"""

import asyncio
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_deliveries import delivery_path, ready_team, upload_delivery
from test_final_review import decide, pending
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, wait_for_records
from test_modules import publish_modules
from test_projects import create_project
from test_specs import publish_spec

from app.models import DeliveryVersion, DeliveryWorkflow, Event, Notification, ProjectMember
from app.services.auth import COOKIE_NAME


def request_path(team, delivery, *, project_id=None):
    return (
        f"/api/projects/{project_id or team.project['id']}/deliveries/{delivery['id']}"
        "/resubmission-requests"
    )


def listed(team, delivery, *, client=None):
    response = (client or team.leader).get(delivery_path(team))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return next(item for item in response.json() if item["id"] == delivery["id"])


def target(binding):
    return {
        "expected_plan_version_id": binding["latest_plan_version_id"],
        "expected_spec_version_id": binding["latest_spec_version_id"],
    }


def adopted_old(environment, *, change="both"):
    team, delivery = pending(environment)
    assert decide(environment, team, delivery)["outcome"] == "adopted"
    if change in {"plan", "both"}:
        team.latest_plan = publish_modules(
            team, team.modules, "修订整体方案", argument="新论证主线"
        )
    else:
        team.latest_plan = team.plan
    if change in {"spec", "both"}:
        team.latest_spec = publish_spec(team.leader, team.project["id"])
    else:
        team.latest_spec = team.spec
    team.binding = listed(team, delivery)["version_binding"]
    return team, delivery


def requests(environment):
    # 主代理负责模型；在 fixture 运行时导入，避免并行落盘期间阻断 test collect。
    from app.models import DeliveryResubmissionRequest

    with environment.sessions() as db:
        return list(db.scalars(select(DeliveryResubmissionRequest)))


@pytest.mark.parametrize("change", ["plan", "spec", "both"])
def test_published_versions_mark_adopted_without_rebinding_or_losing_download(
    auth_environment, change
):
    team, delivery = adopted_old(auth_environment, change=change)
    for client in (team.leader, team.member, team.other_member):
        result = listed(team, delivery, client=client)
        binding = result["version_binding"]
        assert result["review_status"] == "adopted"
        assert result["plan_version_id"] == delivery["plan_version_id"]
        assert result["spec_version_id"] == delivery["spec_version_id"]
        assert binding["bound_plan_version"] == team.plan["version"]
        assert binding["bound_spec_version"] == team.spec["version"]
        assert binding["latest_plan_version_id"] == team.latest_plan["id"]
        assert binding["latest_spec_version_id"] == team.latest_spec["id"]
        assert binding["latest_plan_version"] == team.latest_plan["version"]
        assert binding["latest_spec_version"] == team.latest_spec["version"]
        assert binding["plan_outdated"] is (change in {"plan", "both"})
        assert binding["spec_outdated"] is (change in {"spec", "both"})
        assert binding["based_on_old_version"] is True
        assert binding["can_request_resubmission"] is (client is team.leader)
        assert binding["resubmission_request"] is None
        assert client.get(result["download_url"]).content == b"proof"


def test_one_click_records_human_request_and_repeated_click_is_idempotent(auth_environment):
    team, delivery = adopted_old(auth_environment)
    body = target(team.binding)
    path = request_path(team, delivery)
    with auth_environment.sessions() as db:
        before_notifications = len(list(db.scalars(select(Notification))))
        before_events = len(list(db.scalars(select(Event))))
        old_job = db.get(DeliveryVersion, UUID(delivery["id"])).review_job
        old_job_id = old_job.id
    first = team.leader.post(path, json=body, headers=CSRF_HEADERS)
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    result = first.json()
    assert result["review_status"] == "adopted"
    binding = result["version_binding"]
    assert not binding["can_request_resubmission"]
    requested = binding["resubmission_request"]
    assert requested["requested_by"] == team.leader_id
    assert requested["target_plan_version_id"] == team.latest_plan["id"]
    assert requested["target_spec_version_id"] == team.latest_spec["id"]
    assert requested["requested_at"]
    repeated = team.leader.post(path, json=body, headers=CSRF_HEADERS)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["version_binding"]["resubmission_request"] == requested
    for client in (team.member, team.other_member):
        assert listed(team, delivery, client=client)["version_binding"]["resubmission_request"] == (
            requested
        )
    with auth_environment.sessions() as db:
        assert len(list(db.scalars(select(Notification)))) == before_notifications + 1
        assert len(list(db.scalars(select(Event)))) == before_events + 1
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "adopted"
        old = db.get(DeliveryVersion, UUID(delivery["id"]))
        assert old.review_job.id == old_job_id
        assert old.plan_version_id == UUID(delivery["plan_version_id"])
        assert old.spec_version_id == UUID(delivery["spec_version_id"])
    rows = requests(auth_environment)
    assert len(rows) == 1
    assert str(rows[0].id) == requested["id"]
    with auth_environment.sessions() as db:
        event = db.get(Event, rows[0].event_id)
        assert event.actor_type == "human" and event.actor_id == team.leader_id
        notification = db.scalar(
            select(Notification)
            .where(Notification.user_id == UUID(team.member_id))
            .order_by(Notification.created_at.desc())
        )
        assert str(rows[0].id) in str(notification.details)


def test_newer_publication_allows_new_request_and_preserves_previous_one(auth_environment):
    team, delivery = adopted_old(auth_environment)
    path = request_path(team, delivery)
    first = team.leader.post(path, json=target(team.binding), headers=CSRF_HEADERS)
    assert first.status_code == 200, first.text
    first_request = first.json()["version_binding"]["resubmission_request"]
    publish_spec(team.leader, team.project["id"])
    binding = listed(team, delivery)["version_binding"]
    assert binding["can_request_resubmission"]
    second = team.leader.post(path, json=target(binding), headers=CSRF_HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["version_binding"]["resubmission_request"]["id"] != first_request["id"]
    assert {str(item.id) for item in requests(auth_environment)} == {
        first_request["id"],
        second.json()["version_binding"]["resubmission_request"]["id"],
    }


def test_module_owners_and_platform_admin_member_cannot_request_resubmission(auth_environment):
    team, delivery = adopted_old(auth_environment)
    admin = auth_environment.make_client(platform_admin_user_ids=team.member_id)
    assert login(admin, "member").json()["is_admin"]
    for client in (team.member, team.other_member, admin):
        response = client.post(
            request_path(team, delivery), json=target(team.binding), headers=CSRF_HEADERS
        )
        assert response.status_code == 403, response.text
        assert not listed(team, delivery, client=client)["version_binding"][
            "can_request_resubmission"
        ]
    assert requests(auth_environment) == []


def test_creator_must_still_be_current_project_leader(auth_environment):
    team, delivery = adopted_old(auth_environment)
    with auth_environment.sessions() as db:
        creator = db.get(ProjectMember, (UUID(team.project["id"]), UUID(team.leader_id)))
        creator.role = "member"
        new_leader = db.get(ProjectMember, (UUID(team.project["id"]), UUID(team.other_member_id)))
        new_leader.role = "leader"
        db.commit()
    response = team.leader.post(
        request_path(team, delivery), json=target(team.binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 403, response.text
    assert not listed(team, delivery)["version_binding"]["can_request_resubmission"]
    response = team.other_member.post(
        request_path(team, delivery), json=target(team.binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["version_binding"]["resubmission_request"]["requested_by"] == (
        team.other_member_id
    )


def test_cross_project_and_unknown_deliveries_are_hidden_even_from_admin(auth_environment):
    team, delivery = adopted_old(auth_environment)
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outside-binding").json()["id"]
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_id)
    assert login(admin, "outside-binding").json()["is_admin"]
    for client in (outsider, admin):
        response = client.post(
            request_path(team, delivery), json=target(team.binding), headers=CSRF_HEADERS
        )
        assert response.status_code == 404
    other_project = create_project(team.leader, "另一个独立项目")
    for project_id, selected in (
        (other_project["id"], delivery),
        (team.project["id"], {"id": str(uuid4())}),
        (str(uuid4()), delivery),
    ):
        response = team.leader.post(
            request_path(team, selected, project_id=project_id),
            json=target(team.binding),
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 404, response.text
    assert requests(auth_environment) == []


def test_resubmission_requires_session_and_same_origin_csrf(auth_environment):
    team, delivery = adopted_old(auth_environment)
    path, body = request_path(team, delivery), target(team.binding)
    assert auth_environment.make_client().post(
        path, json=body, headers=CSRF_HEADERS
    ).status_code == (401)
    for headers in ({}, {**CSRF_HEADERS, "Origin": "https://outside.example"}):
        response = team.leader.post(path, json=body, headers=headers)
        assert response.status_code == 403
    assert requests(auth_environment) == []


@pytest.mark.parametrize("state", ["ai_reviewing", "pending_final_review", "final_returned"])
def test_only_adopted_deliveries_can_be_requested_after_publication(auth_environment, state):
    if state == "ai_reviewing":
        team = ready_team(auth_environment)
        response = upload_delivery(team.member, team)
        assert response.status_code == 201
        delivery = response.json()
    else:
        team, delivery = pending(auth_environment)
        if state == "final_returned":
            decide(auth_environment, team, delivery, decision="return", comment="请补充依据")
    publish_spec(team.leader, team.project["id"])
    result = listed(team, delivery)
    assert result["review_status"] == state
    binding = result["version_binding"]
    assert binding["spec_outdated"] and not binding["based_on_old_version"]
    assert not binding["can_request_resubmission"]
    response = team.leader.post(
        request_path(team, delivery), json=target(binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 409, response.text
    assert requests(auth_environment) == []


def test_current_adopted_delivery_has_no_outdated_flag_or_rework_operation(auth_environment):
    team, delivery = pending(auth_environment)
    decide(auth_environment, team, delivery)
    binding = listed(team, delivery)["version_binding"]
    assert not any(
        binding[key]
        for key in (
            "plan_outdated",
            "spec_outdated",
            "based_on_old_version",
            "can_request_resubmission",
        )
    )
    response = team.leader.post(
        request_path(team, delivery), json=target(binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 409, response.text
    assert requests(auth_environment) == []


@pytest.mark.parametrize("change", ["plan", "spec"])
def test_stale_target_ids_require_refresh_without_sending_a_request(auth_environment, change):
    team, delivery = adopted_old(auth_environment)
    old_body = target(team.binding)
    if change == "plan":
        publish_modules(team, team.modules, "再次更新方案")
    else:
        publish_spec(team.leader, team.project["id"])
    response = team.leader.post(request_path(team, delivery), json=old_body, headers=CSRF_HEADERS)
    assert response.status_code == 409, response.text
    assert requests(auth_environment) == []
    binding = listed(team, delivery)["version_binding"]
    assert binding["can_request_resubmission"]
    assert (
        team.leader.post(
            request_path(team, delivery), json=target(binding), headers=CSRF_HEADERS
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_plan_version_id": None},
        {"expected_plan_version_id": True},
        {"expected_plan_version_id": "invalid-uuid"},
        {"expected_spec_version_id": []},
        {"expected_spec_version_id": ""},
        {"requested_by": str(uuid4())},
        {"state": "returned"},
        {"project_id": str(uuid4())},
    ],
)
def test_invalid_or_forged_request_fields_never_write_records(auth_environment, changes):
    team, delivery = adopted_old(auth_environment)
    body = {**target(team.binding), **changes}
    response = team.leader.post(request_path(team, delivery), json=body, headers=CSRF_HEADERS)
    assert response.status_code == 422, response.text
    assert requests(auth_environment) == []


def test_missing_expected_versions_cannot_request_resubmission(auth_environment):
    team, delivery = adopted_old(auth_environment)
    response = team.leader.post(request_path(team, delivery), json={}, headers=CSRF_HEADERS)
    assert response.status_code == 422, response.text
    assert requests(auth_environment) == []


@pytest.mark.parametrize("owner", ["leader", "member"])
@pytest.mark.parametrize("with_cookie", [False, True])
def test_agent_cannot_request_resubmission_even_with_owners_cookie(
    auth_environment, owner, with_cookie
):
    team, delivery = adopted_old(auth_environment)
    owner_client = getattr(team, owner)
    created = register_agent(owner_client, team.project["id"], f"{owner}的助手")
    caller = owner_client if with_cookie else auth_environment.make_client()
    response = caller.post(
        request_path(team, delivery),
        json=target(team.binding),
        headers={**CSRF_HEADERS, **bearer(created["token"])},
    )
    assert response.status_code == 403, response.text
    assert response.headers["cache-control"] == "no-store"
    assert requests(auth_environment) == []
    with auth_environment.sessions() as db:
        events = list(
            db.scalars(select(Event).where(Event.event_type == "agent_permission_denied"))
        )
        assert len(events) == 1
        assert events[0].actor_id == created["agent"]["id"]
        assert events[0].details["action"] == "request_resubmission"
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "adopted"


@pytest.mark.parametrize("with_cookie", [False, True])
def test_mcp_has_no_resubmission_tool_even_for_leader_with_cookie(
    auth_environment, mcp_network, with_cookie
):
    team, delivery = adopted_old(auth_environment)
    created = register_agent(team.leader, team.project["id"], "负责人的重交助手")
    headers = (
        {"Cookie": f"{COOKIE_NAME}={team.leader.cookies.get(COOKIE_NAME)}"} if with_cookie else {}
    )

    async def scenario():
        async with official_client(mcp_network, created["token"], headers=headers) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "request_resubmission" not in tools
            result = await client.call_tool(
                "request_resubmission",
                {
                    "project_id": team.project["id"],
                    "delivery_id": delivery["id"],
                    **target(team.binding),
                },
            )
            assert result.is_error

    asyncio.run(scenario())
    assert requests(auth_environment) == []
    calls = wait_for_records(auth_environment, team, "agent_tool_call", 1)
    assert len(calls) == 1
    assert calls[0].details == {"tool": "unknown", "outcome": "error"}
    with auth_environment.sessions() as db:
        denials = list(
            db.scalars(select(Event).where(Event.event_type == "agent_permission_denied"))
        )
        assert len(denials) == 1
        assert denials[0].actor_id == created["agent"]["id"]
        assert denials[0].details["action"] == "mcp_unknown_tool"
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "adopted"


def test_resubmission_creates_new_bound_version_and_keeps_old_adopted(auth_environment):
    team, delivery = adopted_old(auth_environment)
    response = team.leader.post(
        request_path(team, delivery), json=target(team.binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    requested = response.json()["version_binding"]["resubmission_request"]
    response = upload_delivery(team.member, team, b"revised-content")
    assert response.status_code == 201, response.text
    new_delivery = response.json()
    assert new_delivery["version"] == delivery["version"] + 1
    assert new_delivery["id"] != delivery["id"]
    assert new_delivery["plan_version_id"] == team.latest_plan["id"]
    assert new_delivery["spec_version_id"] == team.latest_spec["id"]
    assert new_delivery["review_status"] == "ai_reviewing"
    assert not new_delivery["version_binding"]["based_on_old_version"]
    old = listed(team, delivery)
    assert old["review_status"] == "adopted"
    assert old["version_binding"]["resubmission_request"] == requested
    assert team.member.get(old["download_url"]).content == b"proof"


def test_removed_module_preserves_old_binding_request_and_download(auth_environment):
    team, delivery = adopted_old(auth_environment)
    remaining = deepcopy(team.modules[1:])
    remaining[0]["upstream"] = []
    publish_modules(team, remaining, "移除已完成模块")
    binding = listed(team, delivery)["version_binding"]
    response = team.leader.post(
        request_path(team, delivery), json=target(binding), headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["review_status"] == "adopted"
    all_deliveries = team.member.get(f"/api/projects/{team.project['id']}/deliveries").json()
    old = next(item for item in all_deliveries if item["id"] == delivery["id"])
    assert old["version_binding"]["resubmission_request"] is not None
    assert team.member.get(old["download_url"]).content == b"proof"
