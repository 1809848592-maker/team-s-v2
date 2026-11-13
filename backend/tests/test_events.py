import csv
import json
from datetime import datetime
from io import StringIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from test_auth import CSRF_HEADERS, PASSWORD, login, register

from app.events import record_event
from app.models import Event, Project
from app.models.event import EventAppendOnlyError
from app.services.auth import COOKIE_NAME


def create_project(client, name="事件测试项目"):
    response = client.post("/api/projects", json={"name": name}, headers=CSRF_HEADERS)
    assert response.status_code == 201
    return response.json()


def join_project(leader, member, project_id):
    response = leader.post(
        f"/api/projects/{project_id}/invitations",
        json={"role": "member"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    token = response.json()["token"]
    response = member.post("/api/invitations/accept", json={"token": token}, headers=CSRF_HEADERS)
    assert response.status_code == 200
    return token


def test_project_events_preserve_actors_order_and_safe_details(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader").json()
    project = create_project(leader)
    member = auth_environment.make_client()
    member_identity = register(member, "member").json()
    raw_invitation = join_project(leader, member, project["id"])

    response = leader.get(f"/api/projects/{project['id']}/events")
    assert response.status_code == 200
    events = response.json()
    assert [event["event_type"] for event in events] == [
        "project_created",
        "invitation_created",
        "member_joined",
    ]
    assert [event["actor_id"] for event in events] == [
        leader_identity["id"],
        leader_identity["id"],
        member_identity["id"],
    ]
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    times = [datetime.fromisoformat(event["occurred_at"]) for event in events]
    assert times == sorted(times)
    for event in events:
        assert event["project_id"] == project["id"]
        assert event["actor_type"] == "human"
        assert event["represented_user_id"] is None
        assert event["plan_version_id"] is None
        assert event["spec_version_id"] is None
        assert event["task_id"] is None
        assert isinstance(event["details"], dict)
    with auth_environment.sessions() as db:
        all_details = json.dumps([event.details for event in db.scalars(select(Event))])
    assert PASSWORD not in all_details
    assert raw_invitation not in all_details
    assert leader.cookies.get(COOKIE_NAME) not in all_details
    assert member.cookies.get(COOKIE_NAME) not in all_details


def test_successful_authentication_changes_have_global_events_without_secrets(auth_environment):
    client = auth_environment.client
    identity = register(client).json()
    registration_token = client.cookies.get(COOKIE_NAME)
    assert login(client).status_code == 200
    login_token = client.cookies.get(COOKIE_NAME)
    assert client.post("/api/auth/logout", headers=CSRF_HEADERS).status_code == 204

    with auth_environment.sessions() as db:
        events = db.scalars(
            select(Event).where(Event.project_id.is_(None)).order_by(Event.id)
        ).all()
        assert [event.event_type for event in events] == [
            "user_registered",
            "user_logged_in",
            "user_logged_out",
        ]
        assert all(event.actor_id == identity["id"] for event in events)
        assert all(event.actor_type == "human" for event in events)
        details = json.dumps([event.details for event in events])
    for secret in (PASSWORD, registration_token, login_token):
        assert secret not in details


def test_activity_filters_combine_without_crossing_projects(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader)
    other_project = create_project(leader, "另一个项目")
    task_id, object_id, agent_id, plan_id, spec_id = (uuid4() for _ in range(5))
    project_id = UUID(project["id"])
    with auth_environment.sessions.begin() as db:
        human_event = record_event(
            db,
            actor_id=identity["id"],
            event_type="progress_reported",
            object_type="task",
            object_id=object_id,
            project_id=project_id,
            task_id=task_id,
            details={"source": "human"},
        )
        agent_event = record_event(
            db,
            actor_id=agent_id,
            actor_type="agent",
            represented_user_id=UUID(identity["id"]),
            event_type="progress_reported",
            object_type="task",
            object_id=object_id,
            project_id=project_id,
            task_id=task_id,
            plan_version_id=plan_id,
            spec_version_id=spec_id,
            details={"source": "agent"},
        )
        record_event(
            db,
            actor_id=identity["id"],
            event_type="progress_reported",
            object_type="task",
            object_id=uuid4(),
            project_id=project_id,
            task_id=uuid4(),
        )
        record_event(
            db,
            actor_id=agent_id,
            actor_type="agent",
            represented_user_id=UUID(identity["id"]),
            event_type="progress_reported",
            object_type="task",
            object_id=object_id,
            project_id=UUID(other_project["id"]),
            task_id=task_id,
        )
        expected_ids = {human_event.id, agent_event.id}
        agent_event_id = agent_event.id

    path = f"/api/projects/{project['id']}/events"
    for params in ({"task_id": str(task_id)}, {"object_id": str(object_id)}):
        response = leader.get(path, params=params)
        assert response.status_code == 200
        assert {event["id"] for event in response.json()} == expected_ids
    response = leader.get(
        path,
        params={"task_id": str(task_id), "object_id": str(object_id), "actor_id": str(agent_id)},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    event = response.json()[0]
    assert event["id"] == agent_event_id
    assert event["represented_user_id"] == identity["id"]
    assert event["plan_version_id"] == str(plan_id)
    assert event["spec_version_id"] == str(spec_id)


def test_complete_json_and_csv_exports_match_beyond_activity_limit(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader, '=HYPERLINK("https://example.invalid")')
    with auth_environment.sessions.begin() as db:
        for index in range(105):
            record_event(
                db,
                actor_id=identity["id"],
                event_type="progress_reported",
                object_type="task",
                object_id=f"task-{index}",
                project_id=UUID(project["id"]),
                details={"summary": f"第 {index} 条，含逗号,与换行\n内容"},
            )

    path = f"/api/projects/{project['id']}/events"
    assert len(leader.get(path).json()) == 100
    assert leader.get(path, params={"limit": 501}).status_code == 422
    json_response = leader.get(f"{path}/export", params={"format": "json"})
    csv_response = leader.get(f"{path}/export", params={"format": "csv"})
    assert json_response.status_code == csv_response.status_code == 200
    assert "application/json" in json_response.headers["content-type"]
    assert "text/csv" in csv_response.headers["content-type"]
    events = json_response.json()
    csv_rows = list(csv.DictReader(StringIO(csv_response.content.decode("utf-8-sig"))))
    assert len(events) == len(csv_rows) == 106
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    for event, row in zip(events, csv_rows, strict=True):
        assert set(row) == set(event)
        for field, value in event.items():
            if field == "details":
                assert json.loads(row[field]) == value
            elif field == "occurred_at":
                assert datetime.fromisoformat(row[field]) == datetime.fromisoformat(value)
            else:
                assert row[field] == ("" if value is None else str(value))
        assert all(not cell.startswith(("=", "+", "-", "@", "\t", "\r")) for cell in row.values())


def test_members_can_read_but_only_leaders_can_export_project_events(auth_environment):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    member = auth_environment.make_client()
    register(member, "member")
    join_project(leader, member, project["id"])
    outsider = auth_environment.make_client()
    outsider_identity = register(outsider, "outsider").json()
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_identity["id"])
    assert login(admin, "outsider").json()["is_admin"] is True
    anonymous = auth_environment.make_client()
    path = f"/api/projects/{project['id']}/events"

    assert member.get(path).status_code == 200
    for format_name in ("json", "csv"):
        export_path = f"{path}/export?format={format_name}"
        assert leader.get(export_path).status_code == 200
        assert member.get(export_path).status_code == 403
        assert anonymous.get(export_path).status_code == 401
        assert outsider.get(export_path).status_code == 404
        assert admin.get(export_path).status_code == 404
    assert anonymous.get(path).status_code == 401
    assert outsider.get(path).status_code == 404
    assert admin.get(path).status_code == 404


def test_events_cannot_be_changed_or_deleted_through_orm_database_or_http(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    path = f"/api/projects/{project['id']}/events"
    original = leader.get(path).json()[0]

    with auth_environment.sessions() as db:
        event = db.get(Event, original["id"])
        event.details = {"changed": True}
        with pytest.raises(EventAppendOnlyError):
            db.flush()
        db.rollback()
        event = db.get(Event, original["id"])
        with pytest.raises(EventAppendOnlyError):
            db.delete(event)
            db.flush()
        db.rollback()
        for statement in (
            update(Event).where(Event.id == original["id"]).values(details={"changed": True}),
            delete(Event).where(Event.id == original["id"]),
        ):
            with pytest.raises(EventAppendOnlyError):
                db.execute(statement)
            db.rollback()
        for statement in (
            update(Event.__table__)
            .where(Event.id == original["id"])
            .values(details={"changed": True}),
            delete(Event.__table__).where(Event.id == original["id"]),
        ):
            with pytest.raises(DBAPIError):
                db.connection().execute(statement)
            db.rollback()

    for target in (path, f"{path}/{original['id']}"):
        for method in ("PUT", "PATCH", "DELETE"):
            response = leader.request(method, target, json={}, headers=CSRF_HEADERS)
            assert response.status_code in (404, 405)
    assert leader.get(path).json() == [original]


def test_record_event_participates_in_the_callers_transaction(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project_id = uuid4()
    with pytest.raises(RuntimeError, match="force transaction rollback"):
        with auth_environment.sessions.begin() as db:
            db.add(Project(id=project_id, name="回滚项目", created_by=UUID(identity["id"])))
            db.flush()
            event = record_event(
                db,
                actor_id=identity["id"],
                event_type="project_created",
                object_type="project",
                object_id=project_id,
                project_id=project_id,
                details={"name": "回滚项目"},
            )
            assert event in db
            raise RuntimeError("force transaction rollback")

    with auth_environment.sessions() as db:
        assert db.get(Project, project_id) is None
        assert db.scalars(select(Event).where(Event.project_id == project_id)).all() == []


PLATFORM_IDENTITIES = {
    "platform-review-worker": "platform_worker",
    "platform-project-linkage-worker": "platform_project_linkage",
    "platform-plan-self-checker": "platform_plan_self_check",
    "platform-spec-drafter": "platform_spec_draft",
}


@pytest.mark.parametrize("actor_id,origin", PLATFORM_IDENTITIES.items())
def test_new_platform_events_record_origin_without_mutating_callers_details(
    auth_environment, actor_id, origin
):
    details = {"summary": "固定替身报告"}
    with auth_environment.sessions.begin() as db:
        event = record_event(
            db,
            actor_type="agent",
            actor_id=actor_id,
            event_type="review_report_accepted",
            object_type="delivery_version",
            object_id=uuid4(),
            details=details,
        )
        assert event.details["execution_origin"] == origin
        assert event.represented_user_id is None
        assert details == {"summary": "固定替身报告"}


def test_actor_kind_filters_legacy_platform_events_before_pagination(auth_environment):
    client = auth_environment.client
    user_id = UUID(register(client).json()["id"])
    project_id = UUID(create_project(client)["id"])
    other_id = UUID(create_project(client, "另一项目")["id"])
    module_id = uuid4()
    with auth_environment.sessions.begin() as db:
        agent = record_event(
            db,
            project_id=project_id,
            actor_type="agent",
            actor_id=uuid4(),
            represented_user_id=user_id,
            event_type="progress_reported",
            object_type="task",
            object_id=module_id,
            task_id=module_id,
            details={"agent_name": "platform-review-worker", "execution_origin": "platform_worker"},
        )
        user_event_id = agent.id
        # Direct inserts reproduce immutable legacy records with missing origins.
        workers = []
        for index in range(108):
            worker = Event(
                project_id=project_id,
                actor_type="agent",
                actor_id=list(PLATFORM_IDENTITIES)[index % 4],
                represented_user_id=None,
                event_type="review_report_accepted",
                object_type="delivery_version",
                object_id=str(module_id),
                task_id=module_id,
                details={},
            )
            db.add(worker)
            db.flush()
            workers.append(worker.id)
        record_event(
            db,
            project_id=other_id,
            actor_type="agent",
            actor_id="platform-review-worker",
            event_type="review_report_accepted",
            object_type="task",
            object_id=module_id,
        )
    path = f"/api/projects/{project_id}/events"
    full_before = client.get(path + "/export").json()
    participant = client.get(path, params={"actor_kind": "participant", "limit": 6})
    assert participant.status_code == 200
    assert [row["id"] for row in participant.json()] == [full_before[0]["id"], user_event_id]
    assert [row["id"] for row in client.get(path, params={"actor_kind": "agent"}).json()] == [
        user_event_id
    ]
    assert [row["id"] for row in client.get(path, params={"actor_kind": "human"}).json()] == [
        full_before[0]["id"]
    ]
    first = client.get(path, params={"actor_kind": "platform"}).json()
    assert [row["id"] for row in first] == workers[-100:]
    older = client.get(path, params={"actor_kind": "platform", "before_id": first[0]["id"]}).json()
    assert [row["id"] for row in older] == workers[:8]
    combined = client.get(
        path,
        params={
            "actor_kind": "platform",
            "task_id": str(module_id),
            "object_id": str(module_id),
            "actor_id": "platform-review-worker",
        },
    ).json()
    assert [row["id"] for row in combined] == workers[::4]
    assert client.get(path, params={"actor_kind": "platform", "task_id": str(uuid4())}).json() == []
    assert client.get(path, params={"actor_kind": "unknown"}).status_code == 422
    assert len(client.get(path).json()) == 100  # Full timeline keeps all actor kinds by default.
    assert client.get(path + "/export").json() == full_before  # Never rewrites historical metadata.
    assert all(row["details"] == {} for row in first + older)
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    assert outsider.get(path, params={"actor_kind": "platform"}).status_code == 404


@pytest.mark.parametrize("actor_type,represented", [("human", False), ("agent", True)])
def test_reserved_name_alone_does_not_make_a_platform_event(
    auth_environment, actor_type, represented
):
    client = auth_environment.client
    user_id = UUID(register(client).json()["id"])
    project_id = UUID(create_project(client)["id"])
    with auth_environment.sessions.begin() as db:
        item = record_event(
            db,
            project_id=project_id,
            actor_type=actor_type,
            actor_id="platform-review-worker",
            represented_user_id=user_id if represented else None,
            event_type="progress_reported",
            object_type="task",
            object_id=uuid4(),
        )
        event_id = item.id
        assert "execution_origin" not in item.details
    path = f"/api/projects/{project_id}/events"
    assert client.get(path, params={"actor_kind": "platform"}).json() == []
    assert event_id in [
        row["id"] for row in client.get(path, params={"actor_kind": "participant"}).json()
    ]
