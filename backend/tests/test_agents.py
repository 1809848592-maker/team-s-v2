import hashlib
import json
import re
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select
from test_auth import CSRF_HEADERS, login, register
from test_modules import create_team
from test_projects import create_project

from app.models import Agent, Event, ProjectMember
from app.services import agents
from app.services.auth import COOKIE_NAME

AGENT_FIELDS = {
    "id",
    "project_id",
    "owner_id",
    "name",
    "client_type",
    "created_at",
    "revoked_at",
}


def agent_path(project_id, agent_id=None):
    path = f"/api/projects/{project_id}/agents"
    return f"{path}/{agent_id}/revoke" if agent_id else path


def register_agent(client, project_id, name="文献助手", client_type="Codex"):
    response = client.post(
        agent_path(project_id),
        json={"name": name, "client_type": client_type},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"agent", "token"}
    assert set(payload["agent"]) == AGENT_FIELDS
    return payload


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def agent_events(environment, project_id, agent_id):
    with environment.sessions() as db:
        return list(
            db.scalars(
                select(Event)
                .where(Event.project_id == UUID(project_id), Event.object_id == agent_id)
                .order_by(Event.id)
            )
        )


def test_member_registers_bound_agent_and_secret_is_only_returned_at_creation(auth_environment):
    team = create_team(auth_environment)
    created = register_agent(team.member, team.project["id"], "  文献助手  ", " 自有客户端 ")
    metadata, token = created["agent"], created["token"]
    assert metadata["owner_id"] == team.member_id
    assert metadata["project_id"] == team.project["id"]
    assert metadata["name"] == "文献助手"
    assert metadata["client_type"] == "自有客户端"
    assert metadata["revoked_at"] is None
    assert datetime.fromisoformat(metadata["created_at"]).tzinfo is not None
    assert re.fullmatch(r"tsv2_agent_[A-Za-z0-9_-]{43}", token)
    digest = hashlib.sha256(token.encode()).hexdigest()

    with auth_environment.sessions() as db:
        stored = db.get(Agent, UUID(metadata["id"]))
        assert stored.project_id == UUID(team.project["id"])
        assert stored.owner_id == UUID(team.member_id)
        assert stored.token_hash == digest
        assert stored.token_hash != token
        rows = db.execute(select(Agent.__table__)).mappings().all()
        assert len(rows) == 1
        assert set(rows[0]) == AGENT_FIELDS | {"token_hash"}
        assert token not in json.dumps(dict(rows[0]), default=str)

    for response in (
        team.member.get(agent_path(team.project["id"])),
        auth_environment.make_client().get("/api/agent/me", headers=bearer(token)),
    ):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() in ([metadata], metadata)
        assert token not in response.text
        assert digest not in response.text
        assert "token_hash" not in response.text


def test_agent_lists_are_private_to_owner_even_for_leader_and_platform_admin(auth_environment):
    team = create_team(auth_environment)
    member_agent = register_agent(team.member, team.project["id"])
    leader_agent = register_agent(team.leader, team.project["id"], "负责人的助手")
    other_agent = register_agent(team.other_member, team.project["id"], "另一成员的助手")
    admin = auth_environment.make_client(platform_admin_user_ids=team.leader_id)
    signed_in = login(admin, "leader")
    assert signed_in.status_code == 200
    assert signed_in.json()["is_admin"] is True

    for client, owned in (
        (team.member, member_agent),
        (team.other_member, other_agent),
        (team.leader, leader_agent),
        (admin, leader_agent),
    ):
        response = client.get(agent_path(team.project["id"]))
        assert response.status_code == 200
        assert response.json() == [owned["agent"]]
        assert set(response.json()[0]) == AGENT_FIELDS
        for created in (member_agent, leader_agent, other_agent):
            assert created["token"] not in response.text
            assert hashlib.sha256(created["token"].encode()).hexdigest() not in response.text
        for forbidden in (member_agent, leader_agent, other_agent):
            if forbidden["agent"]["owner_id"] != owned["agent"]["owner_id"]:
                assert (
                    client.post(
                        agent_path(team.project["id"], forbidden["agent"]["id"]),
                        headers=CSRF_HEADERS,
                    ).status_code
                    == 404
                )


def test_agent_management_is_scoped_by_session_project_and_owner(auth_environment):
    team = create_team(auth_environment)
    created = register_agent(team.member, team.project["id"])
    agent_id = created["agent"]["id"]
    foreign_project = create_project(team.member, "成员自己的另一项目")
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    admin_outsider = auth_environment.make_client(platform_admin_user_ids=outsider_id)
    assert login(admin_outsider, "outsider").json()["is_admin"] is True

    for client in (outsider, admin_outsider):
        assert client.get(agent_path(team.project["id"])).status_code == 404
        assert (
            client.post(
                agent_path(team.project["id"]),
                json={"name": "不应登记", "client_type": "Codex"},
                headers=CSRF_HEADERS,
            ).status_code
            == 404
        )
        assert (
            client.post(agent_path(team.project["id"], agent_id), headers=CSRF_HEADERS).status_code
            == 404
        )
    for project_id, selected_agent in (
        (foreign_project["id"], agent_id),
        (team.project["id"], str(uuid4())),
        (str(uuid4()), agent_id),
    ):
        assert (
            team.member.post(
                agent_path(project_id, selected_agent), headers=CSRF_HEADERS
            ).status_code
            == 404
        )
    assert team.member.get(agent_path(foreign_project["id"])).json() == []
    assert team.member.get(agent_path(str(uuid4()))).status_code == 404

    anonymous = auth_environment.make_client()
    for auth_headers, expected_status in (({}, 401), (bearer(created["token"]), 403)):
        assert (
            anonymous.get(agent_path(team.project["id"]), headers=auth_headers).status_code
            == expected_status
        )
        assert (
            anonymous.post(
                agent_path(team.project["id"]),
                json={"name": "不应登记", "client_type": "Codex"},
                headers={**CSRF_HEADERS, **auth_headers},
            ).status_code
            == expected_status
        )
        assert (
            anonymous.post(
                agent_path(team.project["id"], agent_id),
                headers={**CSRF_HEADERS, **auth_headers},
            ).status_code
            == expected_status
        )
    assert anonymous.get("/api/agent/me", headers=bearer(created["token"])).status_code == 200


def test_same_name_can_register_multiple_independent_agents(auth_environment):
    team = create_team(auth_environment)
    first = register_agent(team.member, team.project["id"])
    second = register_agent(team.member, team.project["id"])
    assert first["agent"]["id"] != second["agent"]["id"]
    assert first["token"] != second["token"]
    response = team.member.get(agent_path(team.project["id"]))
    assert {row["id"] for row in response.json()} == {first["agent"]["id"], second["agent"]["id"]}
    assert len(response.json()) == 2
    assert all(row["name"] == "文献助手" for row in response.json())
    with auth_environment.sessions() as db:
        stored = db.scalars(select(Agent)).all()
        assert len({row.token_hash for row in stored}) == 2
    anonymous = auth_environment.make_client()
    for created in (first, second):
        assert (
            anonymous.get("/api/agent/me", headers=bearer(created["token"])).json()
            == created["agent"]
        )


def test_agent_identity_requires_bearer_and_never_falls_back_to_human_cookie(auth_environment):
    team = create_team(auth_environment)
    created = register_agent(team.member, team.project["id"])
    token = created["token"]
    session_token = team.member.cookies.get(COOKIE_NAME)
    unknown = "tsv2_agent_" + "x" * 43
    invalid_headers = (
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic " + token},
        {"Authorization": "Token " + token},
        bearer("forged-token"),
        bearer(unknown),
        bearer(token + "extra"),
        bearer(session_token),
    )
    anonymous = auth_environment.make_client()
    failures = []
    for client in (anonymous, team.member):
        for headers in invalid_headers:
            response = client.get("/api/agent/me", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.headers["cache-control"] == "no-store"
            failures.append(response.json())
            assert token not in response.text
            assert session_token not in response.text
    assert all(payload == failures[0] for payload in failures)
    # A different logged-in human cannot change the identity selected by a valid token.
    response = team.leader.get("/api/agent/me", headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == created["agent"]
    assert response.json()["owner_id"] == team.member_id


def test_revocation_is_immediate_idempotent_and_preserves_metadata(auth_environment):
    team = create_team(auth_environment)
    created = register_agent(team.member, team.project["id"])
    other = register_agent(team.member, team.project["id"], "独立助手")
    agent_id, token = created["agent"]["id"], created["token"]
    anonymous = auth_environment.make_client()
    assert anonymous.get("/api/agent/me", headers=bearer(token)).status_code == 200
    first = team.member.post(agent_path(team.project["id"], agent_id), headers=CSRF_HEADERS)
    assert first.status_code == 200
    assert set(first.json()) == AGENT_FIELDS
    revoked = first.json()
    assert revoked["revoked_at"] is not None
    assert datetime.fromisoformat(revoked["revoked_at"]).tzinfo is not None
    assert {key: value for key, value in revoked.items() if key != "revoked_at"} == {
        key: value for key, value in created["agent"].items() if key != "revoked_at"
    }
    assert first.headers["cache-control"] == "no-store"
    assert token not in first.text
    assert anonymous.get("/api/agent/me", headers=bearer(token)).status_code == 401
    second = team.member.post(agent_path(team.project["id"], agent_id), headers=CSRF_HEADERS)
    assert second.status_code == 200
    assert second.json() == revoked
    rows = team.member.get(agent_path(team.project["id"])).json()
    assert revoked in rows
    assert other["agent"] in rows
    assert anonymous.get("/api/agent/me", headers=bearer(other["token"])).status_code == 200
    events = agent_events(auth_environment, team.project["id"], agent_id)
    assert sum(event.event_type == "agent_revoked" for event in events) == 1
    with auth_environment.sessions() as db:
        stored = db.get(Agent, UUID(agent_id))
        assert stored.revoked_at == datetime.fromisoformat(revoked["revoked_at"])
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()


def test_agent_writes_require_csrf_and_creation_forbids_spoofing_and_invalid_fields(
    auth_environment,
):
    team = create_team(auth_environment)
    path = agent_path(team.project["id"])
    body = {"name": "文献助手", "client_type": "Codex"}
    invalid = (
        {},
        {**body, "owner_id": team.leader_id},
        {**body, "project_id": str(uuid4())},
        {**body, "token": "chosen-token"},
        {**body, "name": ""},
        {**body, "name": "   "},
        {**body, "name": "名" * 121},
        {**body, "name": "助手\x00名称"},
        {**body, "name": "\n助手"},
        {**body, "name": "助手\x7f"},
        {**body, "client_type": ""},
        {**body, "client_type": "   "},
        {**body, "client_type": "x" * 81},
        {**body, "client_type": "Codex\x00"},
        {**body, "client_type": "客户端\t类型"},
    )
    for payload in invalid:
        response = team.member.post(path, json=payload, headers=CSRF_HEADERS)
        assert response.status_code == 422
        assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])
    assert team.member.get(path).json() == []
    created = register_agent(team.member, team.project["id"], "名" * 120, "x" * 80)
    revoke_path = agent_path(team.project["id"], created["agent"]["id"])
    for headers in (
        {},
        {"X-CSRF-Protection": "0"},
        {**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
    ):
        assert team.member.post(path, json=body, headers=headers).status_code == 403
        assert team.member.post(revoke_path, headers=headers).status_code == 403
    assert team.member.get(path).json() == [created["agent"]]
    assert (
        auth_environment.make_client()
        .get("/api/agent/me", headers=bearer(created["token"]))
        .status_code
        == 200
    )
    with auth_environment.sessions() as db:
        assert len(db.scalars(select(Agent)).all()) == 1
        assert (
            len(db.scalars(select(Event).where(Event.event_type == "agent_registered")).all()) == 1
        )
        assert db.scalars(select(Event).where(Event.event_type == "agent_revoked")).all() == []


def test_authentication_rechecks_revocation_and_owner_membership_with_cached_objects(
    auth_environment,
):
    team = create_team(auth_environment)
    first = register_agent(team.member, team.project["id"])
    second = register_agent(team.member, team.project["id"], "第二助手")
    project_id, member_id = UUID(team.project["id"]), UUID(team.member_id)
    with auth_environment.sessions() as cached_db:
        cached_first = cached_db.get(Agent, UUID(first["agent"]["id"]))
        cached_second = cached_db.get(Agent, UUID(second["agent"]["id"]))
        cached_member = cached_db.get(ProjectMember, (project_id, member_id))
        assert cached_member is not None
        assert agents.authenticate_agent(cached_db, first["token"]) is cached_first
        assert agents.authenticate_agent(cached_db, second["token"]) is cached_second
        response = team.member.post(
            agent_path(team.project["id"], first["agent"]["id"]), headers=CSRF_HEADERS
        )
        assert response.status_code == 200
        with pytest.raises(agents.AgentUnauthenticatedError):
            agents.authenticate_agent(cached_db, first["token"])
        assert agents.authenticate_agent(cached_db, second["token"]) is cached_second
        # This is isolated test data; there is no member-removal product operation in M2-01.
        with auth_environment.sessions() as external_db:
            external_db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == member_id
                )
            )
            external_db.commit()
        assert cached_member in cached_db
        with pytest.raises(agents.AgentUnauthenticatedError):
            agents.authenticate_agent(cached_db, second["token"])
    assert (
        auth_environment.make_client()
        .get("/api/agent/me", headers=bearer(second["token"]))
        .status_code
        == 401
    )


def test_agent_lifecycle_and_identity_events_record_real_actors_without_secrets(auth_environment):
    team = create_team(auth_environment)
    created = register_agent(team.member, team.project["id"])
    metadata, token = created["agent"], created["token"]
    assert (
        auth_environment.make_client().get("/api/agent/me", headers=bearer(token)).status_code
        == 200
    )
    assert (
        team.member.post(
            agent_path(team.project["id"], metadata["id"]), headers=CSRF_HEADERS
        ).status_code
        == 200
    )
    events = agent_events(auth_environment, team.project["id"], metadata["id"])
    assert [event.event_type for event in events] == [
        "agent_registered",
        "agent_identity_read",
        "agent_revoked",
    ]
    for event in events:
        assert event.project_id == UUID(team.project["id"])
        assert event.object_type == "agent"
        assert event.object_id == metadata["id"]
        assert event.task_id is event.plan_version_id is event.spec_version_id is None
        if event.event_type == "agent_identity_read":
            assert event.actor_type == "agent"
            assert event.actor_id == metadata["id"]
            assert event.represented_user_id == UUID(team.member_id)
        else:
            assert event.actor_type == "human"
            assert event.actor_id == team.member_id
            assert event.represented_user_id is None
            assert event.details == {
                "name": metadata["name"],
                "client_type": metadata["client_type"],
            }
        assert token not in json.dumps(event.details)
        assert hashlib.sha256(token.encode()).hexdigest() not in json.dumps(event.details)
    exported = team.leader.get(
        f"/api/projects/{team.project['id']}/events/export", params={"format": "json"}
    )
    assert exported.status_code == 200
    assert token not in exported.text
    assert hashlib.sha256(token.encode()).hexdigest() not in exported.text
    assert "token_hash" not in exported.text
    identity = next(row for row in exported.json() if row["event_type"] == "agent_identity_read")
    assert identity["actor_type"] == "agent"
    assert identity["actor_id"] == metadata["id"]
    assert identity["represented_user_id"] == team.member_id
