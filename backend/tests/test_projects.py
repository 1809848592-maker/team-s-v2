from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Invitation, Project, ProjectMember
from app.security import token_hash
from app.services import projects

CSRF_HEADERS = {"X-CSRF-Protection": "1"}
PASSWORD = "project-test-password"


def register(client: TestClient, username: str):
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": PASSWORD},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def create_project(client: TestClient, name: str = "科研协作项目"):
    response = client.post("/api/projects", json={"name": name}, headers=CSRF_HEADERS)
    assert response.status_code == 201
    return response.json()


def invite(client: TestClient, project_id: str, role: str = "member"):
    response = client.post(
        f"/api/projects/{project_id}/invitations",
        json={"role": role},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    invitation = response.json()
    assert set(invitation) == {"token", "join_path"}
    assert invitation["join_path"] == f"/?invite={invitation['token']}"
    return invitation["token"]


def accept(client: TestClient, token: str):
    return client.post("/api/invitations/accept", json={"token": token}, headers=CSRF_HEADERS)


def member_roles(client: TestClient, project_id: str):
    response = client.get(f"/api/projects/{project_id}/members")
    assert response.status_code == 200
    return {member["user_id"]: member["role"] for member in response.json()}


def test_project_routes_require_session_and_protect_writes(auth_environment):
    anonymous = auth_environment.client
    project_id = str(uuid4())
    for path in (
        "/api/projects",
        f"/api/projects/{project_id}",
        f"/api/projects/{project_id}/members",
    ):
        assert anonymous.get(path).status_code == 401
    for path, payload in (
        ("/api/projects", {"name": "未登录项目"}),
        (f"/api/projects/{project_id}/invitations", {"role": "member"}),
        ("/api/invitations/accept", {"token": "unknown-token"}),
    ):
        assert anonymous.post(path, json=payload, headers=CSRF_HEADERS).status_code == 401

    leader = auth_environment.make_client()
    register(leader, "leader")
    project = create_project(leader)
    token = invite(leader, project["id"])
    joiner = auth_environment.make_client()
    register(joiner, "joiner")

    for client, path, payload in (
        (leader, "/api/projects", {"name": "不应被创建"}),
        (leader, f"/api/projects/{project['id']}/invitations", {"role": "member"}),
        (joiner, "/api/invitations/accept", {"token": token}),
    ):
        assert client.post(path, json=payload).status_code == 403
        assert (
            client.post(
                path,
                json=payload,
                headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
    assert accept(joiner, token).status_code == 200
    assert len(leader.get("/api/projects").json()) == 1


def test_creator_is_leader_and_can_see_only_their_projects(auth_environment):
    leader = auth_environment.client
    identity = register(leader, "leader")
    assert leader.get("/api/projects").json() == []
    project = create_project(leader, "论文小组")

    assert project["name"] == "论文小组"
    assert project["created_by"] == identity["id"]
    assert project["role"] == "leader"
    assert leader.get(f"/api/projects/{project['id']}").json() == project
    assert leader.get("/api/projects").json() == [project]
    assert leader.get(f"/api/projects/{project['id']}/members").json() == [
        {"user_id": identity["id"], "username": "leader", "role": "leader"}
    ]

    other = auth_environment.make_client()
    register(other, "other")
    other_project = create_project(other, "另一个团队")
    assert [item["id"] for item in other.get("/api/projects").json()] == [other_project["id"]]
    assert leader.get("/api/projects").json() == [project]


def test_nonmembers_including_platform_admin_have_no_project_access(auth_environment):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    outsider = auth_environment.make_client()
    outsider_identity = register(outsider, "outsider")
    other_project = create_project(outsider)

    admin = auth_environment.make_client(platform_admin_user_ids=outsider_identity["id"])
    response = admin.post(
        "/api/auth/login",
        json={"username": "outsider", "password": PASSWORD},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["is_admin"] is True

    for client, forbidden_project in (
        (outsider, project),
        (admin, project),
        (leader, other_project),
    ):
        prefix = f"/api/projects/{forbidden_project['id']}"
        assert client.get(prefix).status_code == 404
        assert client.get(f"{prefix}/members").status_code == 404
        assert (
            client.post(
                f"{prefix}/invitations", json={"role": "leader"}, headers=CSRF_HEADERS
            ).status_code
            == 404
        )
        assert forbidden_project["id"] not in {
            item["id"] for item in client.get("/api/projects").json()
        }


def test_invitation_can_be_used_only_once(auth_environment):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    token = invite(leader, project["id"])
    first = auth_environment.make_client()
    first_identity = register(first, "first")
    second = auth_environment.make_client()
    second_identity = register(second, "second")

    response = accept(first, token)
    assert response.status_code == 200
    assert response.json()["project"]["id"] == project["id"]
    assert response.json()["project"]["role"] == "member"
    assert response.json()["membership"] == {
        "user_id": first_identity["id"],
        "username": "first",
        "role": "member",
    }
    assert first.get(f"/api/projects/{project['id']}").status_code == 200
    reused = accept(second, token)
    invalid = accept(second, "unknown-token")
    assert reused.status_code == invalid.status_code == 404
    assert reused.json() == invalid.json()
    assert accept(first, token).status_code == 404
    assert second.get(f"/api/projects/{project['id']}").status_code == 404
    assert second_identity["id"] not in member_roles(leader, project["id"])


def test_project_can_have_multiple_leaders(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader")
    project = create_project(leader)
    new_leader = auth_environment.make_client()
    new_leader_identity = register(new_leader, "new-leader")

    response = accept(new_leader, invite(leader, project["id"], "leader"))
    assert response.status_code == 200
    assert response.json()["membership"]["role"] == "leader"
    assert member_roles(new_leader, project["id"]) == {
        leader_identity["id"]: "leader",
        new_leader_identity["id"]: "leader",
    }

    member = auth_environment.make_client()
    member_identity = register(member, "member")
    assert accept(member, invite(new_leader, project["id"])).status_code == 200
    assert member_roles(leader, project["id"])[member_identity["id"]] == "member"


def test_concurrent_invitation_acceptance_adds_only_one_member(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader")
    project = create_project(leader)
    token = invite(leader, project["id"])
    contenders = []
    for username in ("first-contender", "second-contender"):
        client = auth_environment.make_client()
        identity = register(client, username)
        contenders.append((client, identity))
    barrier = Barrier(2)

    def concurrent_accept(client):
        barrier.wait(timeout=5)
        return accept(client, token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [
            (identity, executor.submit(concurrent_accept, client))
            for client, identity in contenders
        ]
        results = [(identity, future.result(timeout=10)) for identity, future in pending]

    assert sorted(response.status_code for _, response in results) == [200, 404]
    winner = next(identity for identity, response in results if response.status_code == 200)
    assert member_roles(leader, project["id"]) == {
        leader_identity["id"]: "leader",
        winner["id"]: "member",
    }


def test_leader_invitation_waits_for_project_lock_before_adding_membership(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader")
    project = create_project(leader)
    token = invite(leader, project["id"], "leader")
    newcomer = auth_environment.make_client()
    newcomer_identity = register(newcomer, "new-leader")
    project_id, newcomer_id = UUID(project["id"]), UUID(newcomer_identity["id"])
    started = Event()
    accepting_pid = []

    def join():
        with auth_environment.sessions() as db:
            accepting_pid.append(db.scalar(select(func.pg_backend_pid())))
            started.set()
            accepted_project, membership = projects.accept_invitation(db, token, newcomer_id)
            return accepted_project.id, membership.role

    with ThreadPoolExecutor(max_workers=1) as executor:
        with auth_environment.sessions() as locking:
            locking.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
            locking_pid = locking.scalar(select(func.pg_backend_pid()))
            pending = executor.submit(join)
            try:
                assert started.wait(timeout=5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    blocked_by = locking.scalar(select(func.pg_blocking_pids(accepting_pid[0])))
                    if locking_pid in blocked_by:
                        break
                    assert not pending.done(), "邀请接受在项目锁释放之前完成了"
                    Event().wait(0.01)
                else:
                    raise AssertionError("邀请接受没有等待已有项目锁")
                assert not pending.done()
                assert locking.get(ProjectMember, (project_id, newcomer_id)) is None
                assert (
                    locking.scalar(
                        select(func.count())
                        .select_from(ProjectMember)
                        .where(
                            ProjectMember.project_id == project_id, ProjectMember.role == "leader"
                        )
                    )
                    == 1
                )
            finally:
                locking.rollback()
        assert pending.result(timeout=10) == (project_id, "leader")
    assert member_roles(leader, project["id"]) == {
        leader_identity["id"]: "leader",
        newcomer_identity["id"]: "leader",
    }


def test_concurrent_cached_leader_invitation_is_rechecked_after_project_lock(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader")
    project = create_project(leader)
    token = invite(leader, project["id"], "leader")
    contenders = []
    for name in ("first-leader", "second-leader"):
        client = auth_environment.make_client()
        contenders.append(register(client, name))
    barrier = Barrier(2)

    def join(identity):
        with auth_environment.sessions() as db:
            cached = db.get(Invitation, token_hash(token))
            assert cached.used_at is None
            barrier.wait(timeout=5)
            try:
                project_item, membership = projects.accept_invitation(
                    db, token, UUID(identity["id"])
                )
                return str(project_item.id), str(membership.user_id), membership.role
            except projects.InvalidInvitationError:
                db.rollback()
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(join, identity) for identity in contenders]
        results = [future.result(timeout=10) for future in futures]
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    assert accepted[0][0] == project["id"] and accepted[0][2] == "leader"
    assert member_roles(leader, project["id"]) == {
        leader_identity["id"]: "leader",
        accepted[0][1]: "leader",
    }


def test_existing_member_cannot_upgrade_or_consume_an_invitation(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader")
    project = create_project(leader)
    member = auth_environment.make_client()
    member_identity = register(member, "member")
    assert accept(member, invite(leader, project["id"])).status_code == 200
    promotion_token = invite(leader, project["id"], "leader")

    assert accept(member, promotion_token).status_code == 409
    assert accept(leader, promotion_token).status_code == 409
    assert member_roles(leader, project["id"]) == {
        leader_identity["id"]: "leader",
        member_identity["id"]: "member",
    }

    newcomer = auth_environment.make_client()
    newcomer_identity = register(newcomer, "newcomer")
    response = accept(newcomer, promotion_token)
    assert response.status_code == 200
    assert response.json()["membership"]["role"] == "leader"
    assert member_roles(leader, project["id"])[newcomer_identity["id"]] == "leader"


def test_members_cannot_invite_or_override_their_role(auth_environment):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    member = auth_environment.make_client()
    member_identity = register(member, "member")
    token = invite(leader, project["id"])

    assert (
        member.post(
            "/api/invitations/accept",
            json={"token": token, "role": "leader"},
            headers=CSRF_HEADERS,
        ).status_code
        == 422
    )
    assert accept(member, token).status_code == 200
    assert member_roles(leader, project["id"])[member_identity["id"]] == "member"
    for role in ("member", "leader"):
        assert (
            member.post(
                f"/api/projects/{project['id']}/invitations",
                json={"role": role},
                headers=CSRF_HEADERS,
            ).status_code
            == 403
        )
    assert (
        member.post(
            "/api/projects",
            json={"name": "不应被创建", "created_by": project["created_by"]},
            headers=CSRF_HEADERS,
        ).status_code
        == 422
    )
    assert (
        leader.post(
            f"/api/projects/{project['id']}/invitations",
            json={"role": "admin"},
            headers=CSRF_HEADERS,
        ).status_code
        == 422
    )
    assert [item["id"] for item in member.get("/api/projects").json()] == [project["id"]]
