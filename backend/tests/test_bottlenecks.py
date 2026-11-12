"""M4-03 卡点从正式交付、退回历史和协作需求计算；独立PG、无模型调用。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_collaboration import action, report, request_path, request_payload
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_final_review import decide
from test_modules import create_team, publish_modules
from test_projects import create_project
from test_review_reports import components, opinion, process, submit

from app.config import Settings
from app.models import Base, DeliveryWorkflow
from app.models.review_workflow import DeliveryStateTransition


def path(team):
    return f"/api/projects/{team.project['id']}/bottlenecks"


def board(team, *, client=None):
    response = (client or team.leader).get(path(team))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def clock(monkeypatch, instant):
    monkeypatch.setattr("app.services.bottlenecks._now", lambda: instant)


def request(team, expected_by, *, target_module=False):
    body = request_payload(team, target_module=target_module)
    body["expected_by"] = expected_by.isoformat() if expected_by is not None else None
    response = team.member.post(request_path(team), json=body, headers=CSRF_HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def return_delivery(environment, team, mode):
    delivery = submit(environment, team)
    if mode == "returned":
        process(environment, team, delivery, components(opinion()))
    else:
        process(environment, team, delivery)
        decide(environment, team, delivery, decision="return", comment="请补充证据再提交")
    with environment.sessions() as db:
        transition = db.scalar(
            select(DeliveryStateTransition).where(
                DeliveryStateTransition.delivery_id == UUID(delivery["id"]),
                DeliveryStateTransition.to_state == mode,
            )
        )
        assert transition is not None
        return delivery, transition.created_at


def test_only_formal_submission_clears_overdue_module_despite_progress_reports(auth_environment):
    team = ready_team(auth_environment)
    modules = deepcopy(team.modules)
    modules[0]["deadline"] = "2020-01-01T00:00:00+08:00"
    modules[1]["deadline"] = None
    publish_modules(team, modules)
    assert report(team.member, team).status_code == 201
    result = board(team)
    assert result["project_id"] == team.project["id"]
    assert result["return_after_days"] == 3
    assert result["items"] == [
        {
            "kind": "submission_overdue",
            "module_id": modules[0]["id"],
            "delivery_id": None,
            "request_id": None,
            "title": modules[0]["name"],
            "since": "2019-12-31T16:00:00Z",
        }
    ]
    assert upload_delivery(team.member, team).status_code == 201
    assert board(team)["items"] == []


@pytest.mark.parametrize("offset,flagged", [(-1, False), (0, False), (1, True)])
def test_deadline_uses_server_utc_and_strictly_exceeded_boundary(
    auth_environment, monkeypatch, offset, flagged
):
    team = ready_team(auth_environment)
    modules = deepcopy(team.modules)
    modules[0]["deadline"] = "2026-12-10T18:00:00+08:00"
    modules[1]["deadline"] = None
    publish_modules(team, modules)
    deadline = datetime(2026, 12, 10, 10, tzinfo=UTC)
    clock(monkeypatch, deadline + timedelta(microseconds=offset))
    result = board(team)
    assert bool(result["items"]) is flagged
    assert datetime.fromisoformat(result["evaluated_at"]) == deadline + timedelta(
        microseconds=offset
    )


def test_current_plan_only_and_no_plan_is_an_empty_board(auth_environment, monkeypatch):
    team = create_team(auth_environment)
    assert board(team)["items"] == []
    from test_modules import team_modules

    team.modules = team_modules(team)
    clock(monkeypatch, datetime(2030, 1, 1, tzinfo=UTC))
    publish_modules(team, team.modules)
    assert len(board(team)["items"]) == 2
    modules = [deepcopy(team.modules[1])]
    modules[0]["upstream"] = []
    modules[0]["deadline"] = None
    publish_modules(team, modules)
    assert board(team)["items"] == []


def test_private_agent_draft_and_shared_material_are_not_formal_submission(
    auth_environment, monkeypatch
):
    team = draft_team(auth_environment)
    clock(monkeypatch, datetime(2030, 1, 1, tzinfo=UTC))
    assert upload_material(team.member, team.project["id"]).status_code == 201
    items = board(team)["items"]
    assert team.modules[1]["id"] in {item["module_id"] for item in items}
    assert confirm(team).status_code == 201
    assert team.modules[1]["id"] not in {item["module_id"] for item in board(team)["items"]}


@pytest.mark.parametrize("mode", ["returned", "final_returned"])
def test_return_uses_actual_transition_time_and_resubmission_clears_old_return(
    auth_environment, monkeypatch, mode
):
    team = ready_team(auth_environment)
    delivery, returned_at = return_delivery(auth_environment, team, mode)
    # A mutable workflow timestamp must not restart the immutable return clock.
    with auth_environment.sessions() as db:
        db.get(DeliveryWorkflow, UUID(delivery["id"])).updated_at = returned_at + timedelta(days=2)
        db.commit()
    due = returned_at + timedelta(days=3)
    clock(monkeypatch, due)
    assert board(team)["items"] == []
    clock(monkeypatch, due + timedelta(microseconds=1))
    items = board(team)["items"]
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "resubmission_overdue"
    assert item["delivery_id"] == delivery["id"]
    assert item["module_id"] == team.modules[0]["id"]
    assert datetime.fromisoformat(item["since"]) == due
    assert upload_delivery(team.member, team, b"revised-proof").status_code == 201
    assert board(team)["items"] == []


def test_return_threshold_uses_deployment_setting(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    _, returned_at = return_delivery(auth_environment, team, "returned")
    clock(monkeypatch, returned_at + timedelta(days=4))
    assert board(team)["items"]
    client = auth_environment.make_client(bottleneck_return_days=7)
    assert login(client, "leader").status_code == 200
    result = board(team, client=client)
    assert result["return_after_days"] == 7 and result["items"] == []
    clock(monkeypatch, returned_at + timedelta(days=7, microseconds=1))
    assert len(board(team, client=client)["items"]) == 1


def test_newly_returned_revision_starts_a_new_clock(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    _, old_return = return_delivery(auth_environment, team, "final_returned")
    new, new_return = return_delivery(auth_environment, team, "returned")
    assert new_return > old_return
    clock(monkeypatch, old_return + timedelta(days=3, microseconds=1))
    assert board(team)["items"] == []
    clock(monkeypatch, new_return + timedelta(days=3, microseconds=1))
    items = board(team)["items"]
    assert len(items) == 1 and items[0]["delivery_id"] == new["id"]


@pytest.mark.parametrize("invalid", [0, -1, "1.5", "abc"])
def test_return_days_must_be_positive_integer(invalid):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, bottleneck_return_days=invalid)


def test_all_submitted_review_states_do_not_revert_to_submission_overdue(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    clock(monkeypatch, datetime(2030, 1, 1, tzinfo=UTC))
    # Before AI work, the formal submission already suppresses deadline alerts.
    assert [i["module_id"] for i in board(team)["items"]] == [team.modules[1]["id"]]
    process(auth_environment, team, delivery, components(failure="reasoning"))
    assert [i["module_id"] for i in board(team)["items"]] == [team.modules[1]["id"]]


def test_adopted_delivery_stays_submitted_after_plan_update(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery)
    decide(auth_environment, team, delivery)
    publish_modules(team, team.modules, "更新整体主线", argument="修订研究主线")
    clock(monkeypatch, datetime(2030, 1, 1, tzinfo=UTC))
    assert [i["module_id"] for i in board(team)["items"]] == [team.modules[1]["id"]]


@pytest.mark.parametrize("target_module", [False, True])
def test_request_alert_follows_open_accept_respond_supplement_resolve(
    auth_environment, monkeypatch, target_module
):
    team = ready_team(auth_environment)
    expected = datetime(2026, 11, 1, tzinfo=UTC)
    clock(monkeypatch, expected + timedelta(days=1))
    item = request(team, expected, target_module=target_module)
    alerts = board(team)["items"]
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["kind"] == "request_overdue" and alert["request_id"] == item["id"]
    assert alert["module_id"] == (team.modules[1]["id"] if target_module else None)
    assert alert["delivery_id"] is None
    assert action(team.other_member, team, item["id"], "accept").status_code == 200
    assert board(team)["items"] == alerts
    assert action(team.other_member, team, item["id"], "respond", "已给出回应").status_code == 200
    assert board(team)["items"] == []
    supplemented = action(team.member, team, item["id"], "supplement", "请再补充来源")
    assert supplemented.status_code == 200 and supplemented.json()["status"] == "accepted"
    assert action(team.member, team, item["id"], "resolve").status_code == 409
    assert board(team)["items"] == alerts
    assert action(team.other_member, team, item["id"], "respond", "已补充来源").status_code == 200
    assert action(team.member, team, item["id"], "resolve").status_code == 200
    assert board(team)["items"] == []


def test_request_without_expected_time_and_future_or_exact_boundary_do_not_alert(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    clock(monkeypatch, now)
    request(team, None)
    request(team, now + timedelta(seconds=1))
    item = request(team, now)
    assert board(team)["items"] == []
    clock(monkeypatch, now + timedelta(microseconds=1))
    assert [i["request_id"] for i in board(team)["items"]] == [item["id"]]


def test_person_requests_work_without_a_plan_and_remain_scoped_to_project(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    now = datetime(2026, 11, 1, tzinfo=UTC)
    clock(monkeypatch, now)
    item = request(team, now - timedelta(seconds=1))
    other = create_project(team.leader)
    other_path = f"/api/projects/{other['id']}"
    response = team.leader.post(
        other_path + "/requests",
        json={
            "title": "另一项目私有标题",
            "content": "另一项目内容",
            "target_user_id": team.leader_id,
            "expected_by": (now - timedelta(days=1)).isoformat(),
        },
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    assert [i["request_id"] for i in board(team)["items"]] == [item["id"]]
    other_result = team.leader.get(other_path + "/bottlenecks").json()
    assert [i["request_id"] for i in other_result["items"]] == [response.json()["id"]]


def test_supplement_before_a_response_keeps_the_current_open_or_accepted_status(
    auth_environment,
):
    team = ready_team(auth_environment)
    item = request(team, datetime(2020, 1, 1, tzinfo=UTC))
    supplemented = action(team.member, team, item["id"], "supplement", "补充原需求")
    assert supplemented.json()["status"] == "open"
    assert action(team.other_member, team, item["id"], "accept").status_code == 200
    supplemented = action(team.member, team, item["id"], "supplement", "继续补充说明")
    assert supplemented.json()["status"] == "accepted"
    assert action(team.member, team, item["id"], "resolve").status_code == 409


def test_leader_only_with_project_isolation_admin_and_agent_channel_boundaries(auth_environment):
    team = ready_team(auth_environment)
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_id)
    assert login(admin, "outsider").status_code == 200
    for client, expected in (
        (auth_environment.make_client(), 401),
        (team.member, 403),
        (team.other_member, 403),
        (outsider, 404),
        (admin, 404),
    ):
        assert client.get(path(team)).status_code == expected
    assert team.leader.get(f"/api/projects/{uuid4()}/bottlenecks").status_code == 404
    token = register_agent(team.leader, team.project["id"])["token"]
    assert team.leader.get(path(team), headers=bearer(token)).status_code == 403


def snapshot(environment):
    with environment.sessions() as db:
        return {
            table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in Base.metadata.sorted_tables
        }


def test_reading_alerts_is_deterministic_and_has_no_database_writes(auth_environment, monkeypatch):
    team = ready_team(auth_environment)
    delivery, returned_at = return_delivery(auth_environment, team, "returned")
    request(team, returned_at - timedelta(days=1), target_module=True)
    clock(monkeypatch, returned_at + timedelta(days=4))
    before = snapshot(auth_environment)
    result = board(team)
    assert result == board(team)
    assert len(result["items"]) == 2
    assert snapshot(auth_environment) == before
    assert any(i["delivery_id"] == delivery["id"] for i in result["items"])
