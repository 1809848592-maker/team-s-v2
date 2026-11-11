"""M3-11: human final decisions and specification rulings reject agent identities."""

import asyncio
import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from test_agent_context import context_team
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS
from test_deliveries import upload_delivery
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, recorded, wait_for_records

from app.services import final_review, projects
from app.services.auth import COOKIE_NAME


@pytest.fixture
def final_team(auth_environment):
    team = context_team(auth_environment)
    response = upload_delivery(team.member, team, module_id=team.modules[1]["id"])
    assert response.status_code == 201, response.text
    team.delivery = response.json()
    team.final_path = (
        f"/api/projects/{team.project['id']}/deliveries/{team.delivery['id']}/review/final"
    )
    leader = register_agent(team.leader, team.project["id"], "负责人助手")
    team.leader_agent_id = UUID(leader["agent"]["id"])
    team.leader_token = leader["token"]
    return team


def ruling(decision="accept", comment="", finding_id=None):
    return {
        "finding_id": str(finding_id or uuid4()),
        "decision": decision,
        "comment": comment,
    }


def body(decision="approve", **changes):
    value = {
        "decision": decision,
        "comment": "请补充规范依据。" if decision == "return" else "",
        "expected_revision": 2,
        "context_hash": "a" * 64,
        "spec_rulings": [],
    }
    value.update(changes)
    return value


def test_final_routes_forward_only_authenticated_person_and_validated_body(final_team, monkeypatch):
    team = final_team
    expected = {
        "context_hash": "b" * 64,
        "workflow_revision": 2,
        "rejected_opinions": [{"reason": "仍需负责人核对。"}],
        "specification_exceptions": [{"finding_id": str(uuid4())}],
    }
    calls = []

    def read(db, project_id, delivery_id, user_id):
        calls.append(("read", project_id, delivery_id, user_id))
        return deepcopy(expected)

    def decide(db, project_id, delivery_id, user_id, request):
        calls.append(("decide", project_id, delivery_id, user_id, request.model_dump(mode="json")))
        return {"actor_id": str(user_id), "state": "adopted"}

    monkeypatch.setattr(final_review, "get_final_review", read)
    monkeypatch.setattr(final_review, "decide_final_review", decide)
    response = team.leader.get(team.final_path)
    assert response.status_code == 200 and response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    request = body(spec_rulings=[ruling()])
    response = team.leader.post(team.final_path, json=request, headers=CSRF_HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["actor_id"] == team.leader_id
    assert calls == [
        ("read", UUID(team.project["id"]), UUID(team.delivery["id"]), UUID(team.leader_id)),
        (
            "decide",
            UUID(team.project["id"]),
            UUID(team.delivery["id"]),
            UUID(team.leader_id),
            request,
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        body(decision="adopted"),
        body(decision="return", comment=" \t\n"),
        body(comment="x" * 4001),
        body(comment="不应回显\x00正文"),
        body(expected_revision=0),
        body(expected_revision=True),
        body(expected_revision="2"),
        body(context_hash="invalid"),
        body(context_hash="a" * 65),
        body(actor_id=str(uuid4())),
        body(self_review=True),
        body(state="adopted"),
        body(spec_rulings=[ruling(decision="unknown")]),
        body(spec_rulings=[ruling(decision="reject", comment="")]),
        body(spec_rulings=[ruling(decision="reject", comment=" \n ")]),
        body(spec_rulings=[ruling(comment="a" * 4001)]),
        body(spec_rulings=[ruling(comment="不应回显\x00裁定")]),
        body(spec_rulings=[ruling(finding_id="not-a-uuid")]),
        body(
            spec_rulings=[
                {"finding_id": str(uuid4()), "decision": "accept", "actor_id": str(uuid4())}
            ]
        ),
        body(spec_rulings=[ruling(finding_id="00000000-0000-0000-0000-000000000001")] * 2),
        body(spec_rulings=[ruling()] * 5001),
    ],
)
def test_invalid_final_request_never_reaches_decision_service(final_team, monkeypatch, payload):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid final-review input reached the service")

    monkeypatch.setattr(final_review, "decide_final_review", forbidden)
    response = final_team.leader.post(final_team.final_path, json=payload, headers=CSRF_HEADERS)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    details = response.json()["detail"]
    assert all("input" not in item and "ctx" not in item for item in details)
    assert "不应回显" not in response.text


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (final_review.FinalReviewNotFoundError, 404),
        (final_review.FinalReviewPermissionError, 403),
        (final_review.FinalReviewConflictError, 409),
        (final_review.FinalReviewValidationError, 422),
        (projects.ProjectNotFoundError, 404),
        (projects.LeaderRequiredError, 403),
    ],
)
def test_final_errors_have_fixed_messages_and_no_sensitive_exception_text(
    final_team, monkeypatch, error, status
):
    def fail(*args, **kwargs):
        raise error("private-final-review-comment-must-not-leak")

    monkeypatch.setattr(final_review, "get_final_review", fail)
    monkeypatch.setattr(final_review, "decide_final_review", fail)
    for response in (
        final_team.leader.get(final_team.final_path),
        final_team.leader.post(final_team.final_path, json=body(), headers=CSRF_HEADERS),
    ):
        assert response.status_code == status
        assert response.headers["cache-control"] == "no-store"
        assert "private-final-review-comment-must-not-leak" not in response.text


def test_final_decision_requires_human_session_and_csrf(auth_environment, final_team, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unauthenticated or cross-origin request reached final review")

    monkeypatch.setattr(final_review, "get_final_review", forbidden)
    monkeypatch.setattr(final_review, "decide_final_review", forbidden)
    anonymous = auth_environment.make_client()
    assert anonymous.get(final_team.final_path).status_code == 401
    assert (
        anonymous.post(final_team.final_path, json=body(), headers=CSRF_HEADERS).status_code == 401
    )
    for headers in ({}, {**CSRF_HEADERS, "Origin": "https://foreign.example"}):
        assert (
            final_team.leader.post(final_team.final_path, json=body(), headers=headers).status_code
            == 403
        )


@pytest.mark.parametrize("owner_role", ["member", "leader"])
@pytest.mark.parametrize("owner_cookie", [False, True])
@pytest.mark.parametrize("action", ["approve", "return", "reject_specification"])
def test_agent_cannot_decide_or_rule_http_even_with_leader_owner_cookie(
    auth_environment, final_team, monkeypatch, owner_role, owner_cookie, action
):
    team = final_team

    def forbidden(*args, **kwargs):
        pytest.fail("Agent crossed the human final-review boundary")

    monkeypatch.setattr(final_review, "decide_final_review", forbidden)
    token = team.leader_token if owner_role == "leader" else team.token
    owner_client = team.leader if owner_role == "leader" else team.member
    owner_id = team.leader_id if owner_role == "leader" else team.member_id
    agent_id = team.leader_agent_id if owner_role == "leader" else team.agent_id
    client = owner_client if owner_cookie else auth_environment.make_client()
    request = body(action if action != "reject_specification" else "approve")
    if action == "reject_specification":
        request["spec_rulings"] = [ruling("reject", "PRIVATE_SPEC_RULING_COMMENT")]
    response = client.post(team.final_path, json=request, headers={**CSRF_HEADERS, **bearer(token)})
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    denials = recorded(auth_environment, team, "agent_permission_denied")
    assert len(denials) == 1
    event = denials[0]
    assert event.actor_type == "agent" and event.actor_id == str(agent_id)
    assert event.represented_user_id == UUID(owner_id)
    assert event.details["action"] == "final_review"
    assert event.details["reason"] == "action_forbidden"
    assert "PRIVATE_SPEC_RULING_COMMENT" not in json.dumps(event.details)


@pytest.mark.parametrize("owner_role", ["member", "leader"])
@pytest.mark.parametrize("owner_cookie", [False, True])
def test_agent_mcp_has_no_final_review_or_spec_ruling_tool_and_records_denial(
    auth_environment, final_team, mcp_network, owner_role, owner_cookie
):
    team = final_team
    token = team.leader_token if owner_role == "leader" else team.token
    owner_client = team.leader if owner_role == "leader" else team.member
    agent_id = team.leader_agent_id if owner_role == "leader" else team.agent_id
    owner_id = team.leader_id if owner_role == "leader" else team.member_id
    headers = (
        {"Cookie": f"{COOKIE_NAME}={owner_client.cookies.get(COOKIE_NAME)}"} if owner_cookie else {}
    )

    async def scenario():
        async with official_client(mcp_network, token, headers=headers) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert not tools & {"final_review", "rule_spec_applicability"}
            for tool, request in (
                ("final_review", body()),
                ("final_review", body("return")),
                ("rule_spec_applicability", ruling("accept")),
                ("rule_spec_applicability", ruling("reject", "PRIVATE_MCP_RULING_COMMENT")),
            ):
                result = await client.call_tool(
                    tool,
                    {
                        "project_id": team.project["id"],
                        "delivery_id": team.delivery["id"],
                        **request,
                    },
                )
                assert result.is_error
                serialized = json.dumps(result.model_dump(mode="json", by_alias=True))
                assert "permission_denied" in serialized
                assert "PRIVATE_MCP_RULING_COMMENT" not in serialized

    asyncio.run(scenario())
    calls = wait_for_records(auth_environment, team, "agent_tool_call", 4)
    assert len(calls) == 4
    assert all(row.details == {"tool": "unknown", "outcome": "error"} for row in calls)
    denials = recorded(auth_environment, team, "agent_permission_denied")
    assert len(denials) == 4
    assert all(row.details["action"] == "mcp_unknown_tool" for row in denials)
    assert all(row.actor_id == str(agent_id) for row in denials)
    assert all(row.represented_user_id == UUID(owner_id) for row in denials)


def test_real_http_final_decision_approves_once_and_preserves_role_boundary(
    auth_environment, final_team
):
    from sqlalchemy import select
    from test_review_reports import components, process

    from app.models import DeliveryWorkflow

    team = final_team
    process(auth_environment, team, team.delivery, components())
    member_context = team.member.get(team.final_path)
    assert member_context.status_code == 200, member_context.text
    assert not member_context.json()["can_decide"]
    context = team.leader.get(team.final_path)
    assert context.status_code == 200, context.text
    snapshot = context.json()
    request = body(
        expected_revision=snapshot["workflow_revision"], context_hash=snapshot["context_hash"]
    )
    assert team.member.post(team.final_path, json=request, headers=CSRF_HEADERS).status_code == 403
    response = team.leader.post(team.final_path, json=request, headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    with auth_environment.sessions() as db:
        state = db.scalar(
            select(DeliveryWorkflow.state).where(
                DeliveryWorkflow.delivery_id == UUID(team.delivery["id"])
            )
        )
        assert state == "adopted"
    assert team.leader.post(team.final_path, json=request, headers=CSRF_HEADERS).status_code == 409
    final = team.member.get(team.final_path)
    assert final.status_code == 200 and final.json()["decision"] is not None


def test_real_http_specification_rejection_returns_delivery_and_stale_handling_is_rejected(
    auth_environment, final_team
):
    from sqlalchemy import select
    from test_review_reports import components, opinion, process

    from app.models import DeliveryWorkflow
    from app.services.review_reports import annotate_finding

    team = final_team
    value = components()
    value["specification"] = {
        "status": "completed",
        "review": {
            "status": "completed",
            "findings": [
                opinion("suggestion", category="specification", reviewer="spec_compliance")
            ],
        },
    }
    report, _, _ = process(auth_environment, team, team.delivery, value)
    finding_id = report["opinions"][0]["finding_id"]
    old = team.leader.get(team.final_path).json()
    with auth_environment.sessions() as db:
        annotate_finding(
            db,
            UUID(team.project["id"]),
            UUID(team.delivery["id"]),
            UUID(finding_id),
            UUID(team.member_id),
            handling="not_adopted",
            reason="此规范不适用于本模块。",
            expected_revision=0,
        )
    stale = body(expected_revision=old["workflow_revision"], context_hash=old["context_hash"])
    assert team.leader.post(team.final_path, json=stale, headers=CSRF_HEADERS).status_code == 409
    snapshot = team.leader.get(team.final_path).json()
    assert snapshot["specification_exceptions"]
    request = body(
        expected_revision=snapshot["workflow_revision"],
        context_hash=snapshot["context_hash"],
        spec_rulings=[ruling("reject", "请按团队规范补充此项。", finding_id=finding_id)],
    )
    response = team.leader.post(team.final_path, json=request, headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    with auth_environment.sessions() as db:
        state = db.scalar(
            select(DeliveryWorkflow.state).where(
                DeliveryWorkflow.delivery_id == UUID(team.delivery["id"])
            )
        )
        assert state == "final_returned"
