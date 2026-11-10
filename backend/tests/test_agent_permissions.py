import hashlib
import json
from copy import copy, deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS
from test_collaboration import action, request_path
from test_deliveries import upload_delivery
from test_modules import create_team, module, module_publication, publish_modules
from test_projects import create_project
from test_specs import publish_spec, specification

from app.events import event_dict
from app.models import Agent, CollaborationRequest, DeliveryVersion, Event, Project
from app.services import agent_permissions as permissions


@pytest.fixture
def permission_team(auth_environment):
    team = create_team(auth_environment)
    ancestor = module("间接上游", [team.other_member_id])
    upstream = module("直接上游", [team.other_member_id])
    upstream["upstream"] = [{"module_id": ancestor["id"], "needs": "上游数据"}]
    owned = module("本人任务", [team.member_id])
    owned["upstream"] = [{"module_id": upstream["id"], "needs": "已采用的结论"}]
    joint = module("共同任务", [team.member_id, team.other_member_id])
    independent = module("独立任务", [team.leader_id])
    team.modules = [ancestor, upstream, owned, joint, independent]
    team.plan = publish_modules(team, team.modules)
    team.spec = publish_spec(team.leader, team.project["id"])
    team.created_agent = register_agent(team.member, team.project["id"])
    team.agent_id = UUID(team.created_agent["agent"]["id"])
    team.token = team.created_agent["token"]
    return team


def denials(environment, agent_id):
    with environment.sessions() as db:
        return list(
            db.scalars(
                select(Event)
                .where(
                    Event.event_type == "agent_permission_denied",
                    Event.actor_id == str(agent_id),
                )
                .order_by(Event.id)
            )
        )


def assert_denial(environment, team, db, action_name, **targets):
    before = {event.id for event in denials(environment, team.agent_id)}
    agent = db.get(Agent, team.agent_id)
    with pytest.raises(permissions.AgentPermissionDeniedError) as failure:
        permissions.authorize(db, agent, action_name, **targets)
    assert failure.value.action == action_name
    assert isinstance(failure.value.reason, str) and failure.value.reason
    # Business callers may roll back on failure; the rejection audit must survive.
    db.rollback()
    added = [event for event in denials(environment, team.agent_id) if event.id not in before]
    assert len(added) == 1
    event = added[0]
    assert event.project_id == UUID(team.project["id"])
    assert event.actor_type == "agent"
    assert event.actor_id == str(team.agent_id)
    assert event.represented_user_id == UUID(team.member_id)
    assert event.details["action"] == action_name
    serialized = json.dumps(event_dict(event), ensure_ascii=False)
    assert team.token not in serialized
    assert hashlib.sha256(team.token.encode()).hexdigest() not in serialized
    assert "token_hash" not in serialized
    return event


def create_request(team, client, *, target_user=None, target_module=None, title="协作输入"):
    response = client.post(
        request_path(team),
        json={
            "title": title,
            "content": "请提供研究输入，不应写入权限拒绝事件全文。",
            "target_user_id": target_user,
            "target_module_id": target_module,
        },
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def submit(client, team, index, content=b"private-delivery-content"):
    response = upload_delivery(client, team, content, module_id=team.modules[index]["id"])
    assert response.status_code == 201
    return response.json()


def test_project_context_reads_and_own_task_list_are_allowed_without_writes(
    auth_environment,
    permission_team,
):
    team = permission_team
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        for action_name in (
            "read_project",
            "read_plan",
            "read_modules",
            "read_spec",
            "read_resources",
            "read_tasks",
            "read_requests",
            "create_request",
        ):
            assert permissions.authorize(db, agent, action_name) is None
            assert (
                permissions.authorize(db, agent, action_name, project_id=UUID(team.project["id"]))
                is None
            )
    assert denials(auth_environment, team.agent_id) == []


def test_every_human_only_action_and_unknown_action_is_denied_and_audited(
    auth_environment,
    permission_team,
):
    team = permission_team
    prohibited = (
        "publish_plan",
        "publish_spec",
        "update_module",
        "mark_complete",
        "mark_passed",
        "submit_delivery",
        "confirm_draft",
        "annotate_review",
        "reject_review_opinion",
        "final_review",
        "resolve_request",
        "read_unsubmitted_content",
        "unknown_future_operation",
    )
    with auth_environment.sessions() as db:
        for action_name in prohibited:
            assert_denial(auth_environment, team, db, action_name)
    assert len(denials(auth_environment, team.agent_id)) == len(prohibited)


def test_task_actions_cover_all_current_owned_modules_and_recheck_reassignment(
    auth_environment,
    permission_team,
):
    team = permission_team
    task_actions = ("read_tasks", "read_task_context", "report_progress", "upload_draft")
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        for index in (2, 3):
            for action_name in task_actions:
                assert (
                    permissions.authorize(
                        db, agent, action_name, module_id=UUID(team.modules[index]["id"])
                    )
                    is None
                )
        for action_name in task_actions:
            for selected in (UUID(team.modules[0]["id"]), uuid4()):
                assert_denial(auth_environment, team, db, action_name, module_id=selected)
        for action_name in task_actions[1:]:
            assert_denial(auth_environment, team, db, action_name)
        revised = deepcopy(team.modules)
        revised[2]["assignee_ids"] = [team.other_member_id]
        publish_modules(team, revised, "本人任务改派")
        for action_name in task_actions:
            assert_denial(
                auth_environment, team, db, action_name, module_id=UUID(team.modules[2]["id"])
            )
            assert (
                permissions.authorize(db, agent, action_name, module_id=UUID(team.modules[3]["id"]))
                is None
            )


def test_own_deliveries_and_reviews_are_readable_but_others_and_missing_versions_fail_closed(
    auth_environment,
    permission_team,
):
    team = permission_team
    own = submit(team.member, team, 2)
    other = submit(team.other_member, team, 3)
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        for action_name in ("read_delivery", "read_review"):
            assert (
                permissions.authorize(db, agent, action_name, delivery_id=UUID(own["id"])) is None
            )
            for selected in (UUID(other["id"]), uuid4()):
                assert_denial(auth_environment, team, db, action_name, delivery_id=selected)
            assert_denial(auth_environment, team, db, action_name)
        assert (
            permissions.delivery_is_adopted(db, db.get(DeliveryVersion, UUID(other["id"]))) is False
        )
        revised = deepcopy(team.modules)
        revised[2]["assignee_ids"] = [team.other_member_id]
        publish_modules(team, revised, "提交后改派任务")
        for action_name in ("read_delivery", "read_review"):
            assert (
                permissions.authorize(db, agent, action_name, delivery_id=UUID(own["id"])) is None
            )


def test_only_adopted_direct_upstream_deliveries_are_readable_and_other_reviews_are_private(
    auth_environment,
    permission_team,
    monkeypatch,
):
    team = permission_team
    ancestor = submit(team.other_member, team, 0)
    upstream = submit(team.other_member, team, 1)
    independent = submit(team.leader, team, 4)
    foreign = copy(team)
    foreign.project = create_project(team.member, "别的项目，内容不应外泄")
    foreign.leader = team.member
    foreign.modules = [module("别的项目模块", [team.member_id])]
    publish_modules(foreign, foreign.modules)
    publish_spec(team.member, foreign.project["id"])
    foreign_version = submit(team.member, foreign, 0, b"foreign-full-text-never-log")
    with auth_environment.sessions() as db:
        assert_denial(auth_environment, team, db, "read_delivery", delivery_id=UUID(upstream["id"]))
        # Exercise the adoption adapter without creating an M3 review state machine.
        monkeypatch.setattr(permissions, "delivery_is_adopted", lambda _db, _delivery: True)
        agent = db.get(Agent, team.agent_id)
        assert (
            permissions.authorize(db, agent, "read_delivery", delivery_id=UUID(upstream["id"]))
            is None
        )
        for version in (ancestor, independent, foreign_version):
            event = assert_denial(
                auth_environment, team, db, "read_delivery", delivery_id=UUID(version["id"])
            )
            assert "foreign-full-text-never-log" not in json.dumps(event_dict(event))
            assert "private-delivery-content" not in json.dumps(event_dict(event))
        assert_denial(auth_environment, team, db, "read_review", delivery_id=UUID(upstream["id"]))
        assert_denial(
            auth_environment,
            team,
            db,
            "read_delivery",
            delivery_id=UUID(upstream["id"]),
            project_id=UUID(foreign.project["id"]),
        )


def test_request_reads_are_limited_to_owner_relationships_and_actions_keep_human_confirmation(
    auth_environment,
    permission_team,
):
    team = permission_team
    created = create_request(team, team.member, target_user=team.other_member_id)
    addressed = create_request(team, team.other_member, target_user=team.member_id)
    module_target = create_request(team, team.other_member, target_module=team.modules[2]["id"])
    unrelated = create_request(team, team.other_member, target_user=team.leader_id)
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        for item in (created, addressed, module_target):
            assert (
                permissions.authorize(db, agent, "read_requests", request_id=UUID(item["id"]))
                is None
            )
        for item in (addressed, module_target):
            assert (
                permissions.authorize(db, agent, "accept_request", request_id=UUID(item["id"]))
                is None
            )
        for action_name in ("read_requests", "accept_request", "respond_request"):
            assert_denial(auth_environment, team, db, action_name, request_id=UUID(unrelated["id"]))
        for action_name in ("accept_request", "respond_request"):
            assert_denial(auth_environment, team, db, action_name, request_id=UUID(created["id"]))
            assert_denial(auth_environment, team, db, action_name)
        assert_denial(
            auth_environment, team, db, "respond_request", request_id=UUID(addressed["id"])
        )
        assert action(team.member, team, addressed["id"], "accept").status_code == 200
        assert (
            permissions.authorize(db, agent, "respond_request", request_id=UUID(addressed["id"]))
            is None
        )
        assert (
            action(team.member, team, addressed["id"], "respond", "输入已准备").status_code == 200
        )
        assert (
            permissions.authorize(db, agent, "respond_request", request_id=UUID(addressed["id"]))
            is None
        )
        assert_denial(
            auth_environment, team, db, "accept_request", request_id=UUID(addressed["id"])
        )
        assert action(team.other_member, team, created["id"], "accept").status_code == 200
        assert (
            action(team.other_member, team, created["id"], "respond", "已提供").status_code == 200
        )
        assert_denial(auth_environment, team, db, "resolve_request", request_id=UUID(created["id"]))
        assert action(team.other_member, team, addressed["id"], "resolve").status_code == 200
        assert_denial(
            auth_environment, team, db, "respond_request", request_id=UUID(addressed["id"])
        )


def test_request_scope_is_live_and_existing_acceptor_retains_only_their_response_role(
    auth_environment,
    permission_team,
):
    team = permission_team
    pending = create_request(team, team.leader, target_module=team.modules[2]["id"])
    accepted = create_request(team, team.leader, target_module=team.modules[2]["id"])
    other_accepted = create_request(team, team.leader, target_module=team.modules[3]["id"])
    assert action(team.member, team, accepted["id"], "accept").status_code == 200
    assert action(team.other_member, team, other_accepted["id"], "accept").status_code == 200
    with auth_environment.sessions() as db:
        agent = db.get(Agent, team.agent_id)
        cached_pending = db.get(CollaborationRequest, UUID(pending["id"]))
        assert (
            permissions.authorize(db, agent, "read_requests", request_id=cached_pending.id) is None
        )
        revised = deepcopy(team.modules)
        revised[2]["assignee_ids"] = [team.other_member_id]
        publish_modules(team, revised, "模块需求目标负责人变更")
        for action_name in ("read_requests", "accept_request", "respond_request"):
            assert_denial(auth_environment, team, db, action_name, request_id=UUID(pending["id"]))
        for action_name in ("read_requests", "respond_request"):
            assert (
                permissions.authorize(db, agent, action_name, request_id=UUID(accepted["id"]))
                is None
            )
        assert_denial(
            auth_environment, team, db, "respond_request", request_id=UUID(other_accepted["id"])
        )


def test_cross_project_and_inactive_agent_denials_survive_business_rollback_without_data_leaks(
    auth_environment,
    permission_team,
):
    team = permission_team
    foreign = create_project(team.member, "其他项目的秘密研究标题")
    with auth_environment.sessions() as db:
        project = db.get(Project, UUID(team.project["id"]))
        project.name = "不应提交的业务修改"
        db.flush()
        event = assert_denial(
            auth_environment, team, db, "read_project", project_id=UUID(foreign["id"])
        )
        assert "其他项目的秘密研究标题" not in json.dumps(event_dict(event), ensure_ascii=False)
        assert "不应提交的业务修改" not in json.dumps(event_dict(event), ensure_ascii=False)
        assert db.get(Project, UUID(team.project["id"])).name == team.project["name"]
        for action_name in ("read_plan", "read_spec", "read_modules", "read_resources"):
            assert_denial(auth_environment, team, db, action_name, project_id=UUID(foreign["id"]))
        agent = db.get(Agent, team.agent_id)
        assert permissions.authorize(db, agent, "read_plan") is None
        assert (
            team.member.post(
                f"/api/projects/{team.project['id']}/agents/{team.agent_id}/revoke",
                headers=CSRF_HEADERS,
            ).status_code
            == 200
        )
        assert_denial(auth_environment, team, db, "read_plan")


def test_bearer_cannot_use_human_read_publish_progress_delivery_or_confirmation_routes(
    auth_environment,
    permission_team,
):
    team = permission_team
    request = create_request(team, team.member, target_user=team.other_member_id)
    assert action(team.other_member, team, request["id"], "accept").status_code == 200
    assert action(team.other_member, team, request["id"], "respond", "已回应").status_code == 200
    prefix = f"/api/projects/{team.project['id']}"
    headers = {**CSRF_HEADERS, **bearer(team.token)}
    before_plan = team.leader.get(f"{prefix}/plan/versions").json()
    before_spec = team.leader.get(f"{prefix}/spec/versions").json()
    before_events = len(denials(auth_environment, team.agent_id))
    responses = [
        team.leader.get(f"{prefix}/plan", headers=headers),
        team.leader.get(f"{prefix}/spec", headers=headers),
        team.leader.get(f"{prefix}/modules", headers=headers),
        team.leader.post(
            f"{prefix}/plan/versions", json=module_publication(team.modules), headers=headers
        ),
        team.leader.post(f"{prefix}/spec/versions", json=specification(), headers=headers),
        team.member.post(
            f"{prefix}/modules/{team.modules[2]['id']}/progress",
            json={"stage": "writing", "summary": "智能体尝试绕过人类通道"},
            headers=headers,
        ),
        upload_delivery(team.member, team, module_id=team.modules[2]["id"], headers=headers),
        team.member.post(request_path(team, request["id"], "resolve"), headers=headers),
    ]
    assert all(response.status_code == 403 for response in responses)
    # The channel boundary runs before CSRF validation, so this still creates a denial audit.
    responses.append(
        team.member.post(request_path(team, request["id"], "resolve"), headers=bearer(team.token))
    )
    assert responses[-1].status_code == 403
    events = denials(auth_environment, team.agent_id)
    assert len(events) - before_events == len(responses)
    for event in events:
        assert event.project_id == UUID(team.project["id"])
        assert event.actor_type == "agent"
        assert event.represented_user_id == UUID(team.member_id)
        assert team.token not in json.dumps(event_dict(event))
    assert team.leader.get(f"{prefix}/plan/versions").json() == before_plan
    assert team.leader.get(f"{prefix}/spec/versions").json() == before_spec
    assert team.member.get(f"{prefix}/modules/{team.modules[2]['id']}/progress").json() == []
    assert team.member.get(f"{prefix}/deliveries").json() == []
    assert team.member.get(request_path(team, request["id"])).json()["status"] == "responded"
    assert action(team.member, team, request["id"], "resolve").status_code == 200


def test_invalid_bearer_cannot_fall_back_to_cookie_and_cross_project_attempts_log_at_origin(
    auth_environment,
    permission_team,
):
    team = permission_team
    prefix = f"/api/projects/{team.project['id']}"
    for credentials in ("tsv2_agent_" + "x" * 43, "bad-token", ""):
        headers = {**CSRF_HEADERS, "Authorization": f"Bearer {credentials}"}
        assert team.leader.get(f"{prefix}/plan", headers=headers).status_code == 401
        assert (
            team.leader.post(
                f"{prefix}/plan/versions", json=module_publication(team.modules), headers=headers
            ).status_code
            == 401
        )
    foreign = create_project(team.member, "其他项目保密内容")
    before = len(denials(auth_environment, team.agent_id))
    response = team.member.get(
        f"/api/projects/{foreign['id']}", headers={**CSRF_HEADERS, **bearer(team.token)}
    )
    assert response.status_code == 403
    events = denials(auth_environment, team.agent_id)
    assert len(events) == before + 1
    assert events[-1].project_id == UUID(team.project["id"])
    assert events[-1].actor_type == "agent"
    assert events[-1].represented_user_id == UUID(team.member_id)
    assert "其他项目保密内容" not in json.dumps(event_dict(events[-1]), ensure_ascii=False)
    assert team.token not in response.text
    assert team.leader.get(f"{prefix}/plan").status_code == 200
