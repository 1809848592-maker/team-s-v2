from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from test_auth import CSRF_HEADERS, register
from test_deliveries import ready_team, upload_delivery
from test_modules import create_team, publish_modules, team_modules
from test_projects import create_project

from app.models import ProgressReport, RequestMessage
from app.models.immutable import SnapshotImmutableError


def progress_path(team, module_id=None):
    selected = module_id or team.modules[0]["id"]
    return f"/api/projects/{team.project['id']}/modules/{selected}/progress"


def report(client, team, stage="writing", summary="完成文献梳理，正在整理研究结论。"):
    return client.post(
        progress_path(team), json={"stage": stage, "summary": summary}, headers=CSRF_HEADERS
    )


def request_path(team, request_id=None, action=None):
    path = f"/api/projects/{team.project['id']}/requests"
    if request_id:
        path += f"/{request_id}"
    if action:
        path += f"/{action}"
    return path


def request_payload(team, *, target_module=False):
    return {
        "title": "请补充数据口径说明",
        "content": "请给出变量定义和处理方法，便于衔接。",
        "target_user_id": None if target_module else team.other_member_id,
        "target_module_id": team.modules[1]["id"] if target_module else None,
        "expected_by": "2026-12-12T18:00:00+08:00",
    }


def create_request(team, *, client=None, target_module=False):
    response = (client or team.member).post(
        request_path(team),
        json=request_payload(team, target_module=target_module),
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def action(client, team, request_id, verb, content=None):
    return client.post(
        request_path(team, request_id, verb),
        json={"content": content} if content is not None else None,
        headers=CSRF_HEADERS,
    )


def test_progress_reports_keep_all_four_stages_and_immutable_human_history(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    plan = publish_modules(team, team.modules)
    before = team.member.get(f"/api/projects/{team.project['id']}/progress").json()
    assert {row["stage"] for row in before} == {"not_started"}
    assert all(row["last_report"] is None and row["delivery_id"] is None for row in before)
    reports = []
    for stage in ("not_started", "planning", "writing", "checking"):
        response = report(team.member, team, stage, f"本次阶段：{stage}")
        assert response.status_code == 201
        entry = response.json()
        reports.append(entry)
        assert entry["user_id"] == team.member_id
        assert entry["source"] == "human"
        assert entry["plan_version_id"] == plan["id"]
        assert entry["project_id"] == team.project["id"]
        assert entry["module_id"] == team.modules[0]["id"]
    history = team.leader.get(progress_path(team)).json()
    assert {entry["id"] for entry in history} == {entry["id"] for entry in reports}
    assert len(history) == 4
    assert history == list(reversed(reports))
    times = [datetime.fromisoformat(entry["created_at"]) for entry in reports]
    assert times == sorted(times)
    current = team.member.get(f"/api/projects/{team.project['id']}/progress").json()
    module_progress = next(row for row in current if row["module_id"] == team.modules[0]["id"])
    assert module_progress == {
        "module_id": team.modules[0]["id"],
        "stage": "checking",
        "last_report": reports[-1],
        "delivery_id": None,
    }
    report_id = UUID(reports[0]["id"])
    with auth_environment.sessions() as db:
        original = db.get(ProgressReport, report_id)
        original.summary = "修改过去的记录"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        with pytest.raises(SnapshotImmutableError):
            db.execute(delete(ProgressReport).where(ProgressReport.id == report_id))
        db.rollback()
        with pytest.raises(DBAPIError):
            db.connection().execute(
                update(ProgressReport.__table__)
                .where(ProgressReport.id == report_id)
                .values(summary="修改过去的记录")
            )
        db.rollback()
    assert team.leader.get(progress_path(team)).json() == history


def test_progress_requires_current_owner_and_rejects_claimed_completion_or_percentage(
    auth_environment,
):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    publish_modules(team, team.modules)
    assert report(team.leader, team).status_code == 403
    assert report(team.other_member, team).status_code == 403
    for payload in (
        {"stage": "submitted", "summary": "声称已交付"},
        {"stage": "complete", "summary": "声称已完成"},
        {"stage": "writing", "summary": "自报百分比", "percent": 100},
        {"stage": "writing", "summary": "伪造来源", "source": "agent"},
        {"stage": "writing", "summary": " "},
        {"stage": "writing", "summary": "x" * 1001},
    ):
        assert (
            team.member.post(progress_path(team), json=payload, headers=CSRF_HEADERS).status_code
            == 422
        )
    assert team.member.get(progress_path(team)).json() == []
    revised = deepcopy(team.modules)
    revised[0]["assignee_ids"] = [team.other_member_id]
    publish_modules(team, revised, "更新模块负责人")
    assert report(team.member, team).status_code == 403
    assert report(team.other_member, team).status_code == 201


def test_formal_delivery_controls_effective_submitted_stage(auth_environment):
    team = ready_team(auth_environment)
    assert report(team.member, team, "checking").status_code == 201
    response = upload_delivery(team.member, team)
    assert response.status_code == 201
    delivery = response.json()
    current = team.member.get(f"/api/projects/{team.project['id']}/progress").json()
    submitted = next(row for row in current if row["module_id"] == team.modules[0]["id"])
    assert submitted["stage"] == "submitted"
    assert submitted["delivery_id"] == delivery["id"]
    assert submitted["last_report"]["stage"] == "checking"
    new_report = report(team.member, team, "planning", "为后续修订补充方案说明。")
    assert new_report.status_code == 201
    refreshed = team.member.get(f"/api/projects/{team.project['id']}/progress").json()
    still_submitted = next(row for row in refreshed if row["module_id"] == team.modules[0]["id"])
    assert still_submitted["stage"] == "submitted"
    assert still_submitted["delivery_id"] == delivery["id"]
    assert still_submitted["last_report"] == new_report.json()


def test_request_target_is_exactly_one_member_or_module_from_this_project(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    publish_modules(team, team.modules)
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    foreign_project = create_project(team.leader, "其他项目")
    other_team = copy(team)
    other_team.project = foreign_project
    foreign_modules = deepcopy(team.modules)
    for entry in foreign_modules:
        entry["id"] = str(uuid4())
        entry["assignee_ids"] = [team.leader_id]
        entry["upstream"] = []
    publish_modules(other_team, foreign_modules)
    base = request_payload(team)
    invalid_targets = (
        ({"target_user_id": None, "target_module_id": None}, 422),
        (
            {"target_user_id": team.other_member_id, "target_module_id": team.modules[1]["id"]},
            422,
        ),
        ({"target_user_id": outsider_id, "target_module_id": None}, 404),
        ({"target_user_id": None, "target_module_id": foreign_modules[0]["id"]}, 404),
        ({"target_user_id": None, "target_module_id": str(uuid4())}, 404),
    )
    for target, status_code in invalid_targets:
        assert (
            team.member.post(
                request_path(team), json=base | target, headers=CSRF_HEADERS
            ).status_code
            == status_code
        )
    assert team.member.get(request_path(team)).json() == []
    for invalid_body in (
        base | {"title": " "},
        base | {"content": ""},
        base | {"expected_by": "2026-12-12T18:00:00"},
        base | {"created_by": team.leader_id},
    ):
        assert (
            team.member.post(
                request_path(team), json=invalid_body, headers=CSRF_HEADERS
            ).status_code
            == 422
        )
    request = create_request(team)
    assert request["status"] == "open"
    assert request["created_by"] == team.member_id
    assert request["messages"] == []
    assert datetime.fromisoformat(request["expected_by"]) == datetime(2026, 12, 12, 10, tzinfo=UTC)
    assert create_request(team, target_module=True)["target_module_id"] == team.modules[1]["id"]


def test_request_closes_only_after_target_response_and_initiator_confirmation(auth_environment):
    team = create_team(auth_environment)
    request = create_request(team)
    request_id = request["id"]
    assert action(team.member, team, request_id, "resolve").status_code == 409
    assert action(team.other_member, team, request_id, "respond", "尚未接手").status_code == 409
    assert action(team.leader, team, request_id, "accept").status_code == 403
    accepted = action(team.other_member, team, request_id, "accept")
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["accepted_by"] == team.other_member_id
    assert action(team.member, team, request_id, "resolve").status_code == 409
    assert action(team.member, team, request_id, "respond", "替对方回应").status_code == 403
    assert action(team.leader, team, request_id, "respond", "替对方回应").status_code == 403
    responded = action(team.other_member, team, request_id, "respond", "已给出数据定义和来源。")
    assert responded.status_code == 200
    assert responded.json()["status"] == "responded"
    assert action(team.other_member, team, request_id, "resolve").status_code == 403
    assert action(team.leader, team, request_id, "resolve").status_code == 403
    resolved = action(team.member, team, request_id, "resolve")
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None
    assert team.leader.get(request_path(team, request_id)).json() == resolved.json()


def test_module_target_acceptance_has_one_winner_under_concurrent_owners(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    team.modules[1]["assignee_ids"] = [team.member_id, team.other_member_id]
    publish_modules(team, team.modules)
    request = create_request(team, client=team.leader, target_module=True)
    for owner in (team.member, team.other_member):
        notices = owner.get("/api/notifications").json()
        request_notices = [
            item for item in notices if item["details"].get("request_id") == request["id"]
        ]
        assert len(request_notices) == 1
        assert request_notices[0]["kind"] == "request_created"
    assert action(team.leader, team, request["id"], "accept").status_code == 403
    barrier = Barrier(2)

    def concurrent_accept(client):
        barrier.wait(timeout=5)
        return action(client, team, request["id"], "accept")

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [
            (team.member, executor.submit(concurrent_accept, team.member)),
            (team.other_member, executor.submit(concurrent_accept, team.other_member)),
        ]
        results = [(client, future.result(timeout=10)) for client, future in pending]
    assert sorted(response.status_code for _, response in results) == [200, 409]
    winner = next(client for client, response in results if response.status_code == 200)
    loser = next(client for client, response in results if response.status_code == 409)
    assert action(loser, team, request["id"], "respond", "不能抢答").status_code == 403
    assert action(winner, team, request["id"], "respond", "由接手人回应").status_code == 200


def test_supplements_and_repeated_responses_remain_in_one_thread_and_resolution_is_final(
    auth_environment,
):
    team = create_team(auth_environment)
    request = create_request(team)
    request_id = request["id"]
    assert action(team.other_member, team, request_id, "accept").status_code == 200
    assert action(team.other_member, team, request_id, "respond", "首次回应").status_code == 200
    assert (
        action(team.member, team, request_id, "supplement", "还需要数据处理规则。").status_code
        == 200
    )
    assert action(team.leader, team, request_id, "supplement", "负责人代补充").status_code == 403
    assert (
        action(team.other_member, team, request_id, "respond", "补充数据处理规则。").status_code
        == 200
    )
    detail = team.member.get(request_path(team, request_id)).json()
    assert [(item["kind"], item["author_id"], item["content"]) for item in detail["messages"]] == [
        ("response", team.other_member_id, "首次回应"),
        ("supplement", team.member_id, "还需要数据处理规则。"),
        ("response", team.other_member_id, "补充数据处理规则。"),
    ]
    assert len(team.member.get(request_path(team)).json()) == 1
    message_id = UUID(detail["messages"][0]["id"])
    with auth_environment.sessions() as db:
        original_message = db.get(RequestMessage, message_id)
        original_message.content = "改写过去的回应"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.connection().execute(
                delete(RequestMessage.__table__).where(RequestMessage.id == message_id)
            )
        db.rollback()
    assert team.member.get(request_path(team, request_id)).json() == detail
    resolved = action(team.member, team, request_id, "resolve").json()
    for client, verb, content in (
        (team.other_member, "accept", None),
        (team.other_member, "respond", "解决后再次回应"),
        (team.member, "supplement", "解决后再次补充"),
    ):
        assert action(client, team, request_id, verb, content).status_code == 409
    assert team.member.get(request_path(team, request_id)).json() == resolved


def test_request_notifications_reach_responsible_people_and_read_without_plan_or_spec_ids(
    auth_environment,
):
    team = create_team(auth_environment)
    request = create_request(team)
    assert team.member.get("/api/notifications").json() == []
    assert team.leader.get("/api/notifications").json() == []
    rows = team.other_member.get("/api/notifications").json()
    assert len(rows) == 1
    assert rows[0]["details"]["request_id"] == request["id"]
    assert rows[0]["kind"] == "request_created"
    assert rows[0]["user_id"] == team.other_member_id
    assert "plan_version_id" not in rows[0]["details"]
    assert "spec_version_id" not in rows[0]["details"]
    response = team.other_member.post(
        f"/api/notifications/{rows[0]['id']}/read", headers=CSRF_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["read_at"] is not None
    assert action(team.other_member, team, request["id"], "accept").status_code == 200
    assert (
        action(team.other_member, team, request["id"], "respond", "已回应需求。").status_code == 200
    )
    requester_notices = team.member.get("/api/notifications").json()
    assert requester_notices
    assert all(row["details"]["request_id"] == request["id"] for row in requester_notices)
    assert action(team.member, team, request["id"], "resolve").status_code == 200
    expected = {"request_accepted", "request_responded", "request_resolved"}
    creator_notices = team.member.get("/api/notifications").json()
    assert {item["kind"] for item in creator_notices} == expected
    assert len(creator_notices) == 3
    target_notices = team.other_member.get("/api/notifications").json()
    assert {item["kind"] for item in target_notices} == expected | {"request_created"}
    assert len(target_notices) == 4
    assert team.leader.get("/api/notifications").json() == []


def test_collaboration_events_preserve_actor_project_and_task_associations(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    plan = publish_modules(team, team.modules)
    assert report(team.member, team).status_code == 201
    request = create_request(team, target_module=True)
    assert action(team.other_member, team, request["id"], "accept").status_code == 200
    assert (
        action(team.other_member, team, request["id"], "respond", "已补齐口径。").status_code == 200
    )
    assert (
        action(team.member, team, request["id"], "supplement", "请再注明来源。").status_code == 200
    )
    assert (
        action(team.other_member, team, request["id"], "respond", "来源已注明。").status_code == 200
    )
    assert action(team.member, team, request["id"], "resolve").status_code == 200
    events = team.leader.get(f"/api/projects/{team.project['id']}/events").json()
    progress_events = [event for event in events if event["event_type"] == "progress_reported"]
    assert len(progress_events) == 1
    assert progress_events[0]["actor_id"] == team.member_id
    assert progress_events[0]["task_id"] == team.modules[0]["id"]
    assert progress_events[0]["plan_version_id"] == plan["id"]
    request_events = [event for event in events if event["object_id"] == request["id"]]
    assert [event["actor_id"] for event in request_events] == [
        team.member_id,
        team.other_member_id,
        team.other_member_id,
        team.member_id,
        team.other_member_id,
        team.member_id,
    ]
    assert all(event["project_id"] == team.project["id"] for event in request_events)
    assert all(event["task_id"] == team.modules[1]["id"] for event in request_events)
    assert all(event["actor_type"] == "human" for event in request_events)


def test_collaboration_routes_enforce_membership_session_and_csrf(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    publish_modules(team, team.modules)
    request = create_request(team)
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    foreign_project = create_project(outsider)
    anonymous = auth_environment.make_client()
    for path in (
        progress_path(team),
        f"/api/projects/{team.project['id']}/progress",
        request_path(team),
        request_path(team, request["id"]),
    ):
        assert outsider.get(path).status_code == 404
        assert anonymous.get(path).status_code == 401
    for client, code in ((outsider, 404), (anonymous, 401)):
        assert report(client, team).status_code == code
        assert (
            client.post(
                request_path(team), json=request_payload(team), headers=CSRF_HEADERS
            ).status_code
            == code
        )
        for verb, content in (
            ("accept", None),
            ("respond", "回应"),
            ("supplement", "补充"),
            ("resolve", None),
        ):
            assert action(client, team, request["id"], verb, content).status_code == code
    for path, payload in (
        (progress_path(team), {"stage": "writing", "summary": "进度"}),
        (request_path(team), request_payload(team)),
        (request_path(team, request["id"], "accept"), None),
        (request_path(team, request["id"], "respond"), {"content": "回应"}),
        (request_path(team, request["id"], "supplement"), {"content": "补充"}),
        (request_path(team, request["id"], "resolve"), None),
    ):
        assert team.member.post(path, json=payload).status_code == 403
        assert (
            team.member.post(
                path,
                json=payload,
                headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
    assert (
        outsider.get(f"/api/projects/{foreign_project['id']}/requests/{request['id']}").status_code
        == 404
    )
