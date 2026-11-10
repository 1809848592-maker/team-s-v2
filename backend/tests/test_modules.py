from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select
from test_auth import CSRF_HEADERS, register
from test_plans import publication, publish
from test_projects import accept, create_project, invite

from app.models import Module


def create_team(environment):
    leader = environment.client
    leader_id = register(leader, "leader").json()["id"]
    project = create_project(leader)
    member = environment.make_client()
    member_id = register(member, "member").json()["id"]
    other_member = environment.make_client()
    other_member_id = register(other_member, "other-member").json()["id"]
    assert accept(member, invite(leader, project["id"])).status_code == 200
    assert accept(other_member, invite(leader, project["id"])).status_code == 200
    return SimpleNamespace(
        leader=leader,
        leader_id=leader_id,
        member=member,
        member_id=member_id,
        other_member=other_member,
        other_member_id=other_member_id,
        project=project,
    )


def module(name, assignee_ids):
    return {
        "id": str(uuid4()),
        "name": name,
        "assignee_ids": assignee_ids,
        "deadline": "2026-12-10T18:00:00+08:00",
        "goal": f"回答{name}的研究问题",
        "core_argument": "",
        "scope_in": "本模块负责的研究内容",
        "scope_out": "其他模块承担的内容",
        "upstream": [],
        "downstream_handoff": "结论、来源和未解决的问题",
        "delivery_format": "docx",
    }


def team_modules(team):
    first = module("文献综述", [team.member_id])
    second = module("研究方法", [team.other_member_id])
    second["upstream"] = [{"module_id": first["id"], "needs": "术语定义与文献摘要"}]
    return [first, second]


def module_publication(modules, note="发布分工", **overall_changes):
    payload = publication(note)
    payload["content"]["modules"] = deepcopy(modules)
    payload["content"].update(overall_changes)
    return payload


def publish_modules(team, modules, note="发布分工", **overall_changes):
    return publish(
        team.leader,
        team.project["id"],
        module_publication(modules, note, **overall_changes),
    )


def notifications(client, project_id, version_id=None):
    response = client.get("/api/notifications", params={"project_id": project_id})
    assert response.status_code == 200
    rows = response.json()
    if version_id is not None:
        rows = [row for row in rows if row["details"]["plan_version_id"] == version_id]
    return rows


def test_multiple_module_owners_and_dependencies_are_frozen_with_each_plan(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    modules[0]["assignee_ids"] = [team.member_id, team.other_member_id]
    first = publish_modules(team, modules)
    path = f"/api/projects/{team.project['id']}/modules"
    response = team.member.get(path)
    assert response.status_code == 200
    actual = {item["id"]: item for item in response.json()}
    assert set(actual) == {item["id"] for item in modules}
    assert actual[modules[0]["id"]]["assignee_ids"] == [team.member_id, team.other_member_id]
    assert actual[modules[1]["id"]]["upstream"] == modules[1]["upstream"]
    assert all(item["plan_version_id"] == first["id"] for item in actual.values())
    assert datetime.fromisoformat(actual[modules[0]["id"]]["deadline"]) == datetime(
        2026, 12, 10, 10, tzinfo=UTC
    )
    for item in actual.values():
        assert team.member.get(f"{path}/{item['id']}").json() == item

    revised = deepcopy(modules)
    revised[0]["goal"] = "修订后的模块问题"
    second = publish_modules(team, revised, "更新模块目标")
    current = team.member.get(f"{path}/{modules[0]['id']}").json()
    assert current["goal"] == revised[0]["goal"]
    assert current["plan_version_id"] == second["id"]
    historical = team.member.get(
        f"/api/projects/{team.project['id']}/plan/versions/{first['id']}"
    ).json()
    assert historical == first
    assert historical["content"]["modules"][0]["goal"] == modules[0]["goal"]


def test_self_cycles_long_cycles_and_unclosed_dependencies_do_not_publish(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    first = publish_modules(team, modules)
    invalid = []
    self_cycle = deepcopy(modules)
    self_cycle[0]["upstream"] = [{"module_id": self_cycle[0]["id"], "needs": "自身输入"}]
    invalid.append(self_cycle)
    cycle = deepcopy(modules)
    cycle.append(module("成果整合", [team.member_id]))
    for index, item in enumerate(cycle):
        item["upstream"] = [{"module_id": cycle[(index + 1) % 3]["id"], "needs": "上游结论"}]
    invalid.append(cycle)
    unclosed = deepcopy(modules)
    unclosed[0]["upstream"] = [{"module_id": str(uuid4()), "needs": "不存在的输入"}]
    invalid.append(unclosed)
    path = f"/api/projects/{team.project['id']}/plan"
    original_notifications = notifications(team.member, team.project["id"])

    for candidate in invalid:
        response = team.leader.post(
            f"{path}/versions", json=module_publication(candidate), headers=CSRF_HEADERS
        )
        assert response.status_code == 422
        assert team.leader.get(path).json() == first
        assert team.leader.get(f"{path}/versions").json() == [first]
        assert notifications(team.member, team.project["id"]) == original_notifications
    with auth_environment.sessions() as db:
        assert (
            len(
                db.scalars(
                    select(Module).where(Module.project_id == UUID(team.project["id"]))
                ).all()
            )
            == 2
        )


def test_foreign_module_ids_and_nonmember_assignees_are_rejected(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    first = publish_modules(team, modules)
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    foreign_project = create_project(team.leader, "另一个项目")
    foreign_module = module("另一个项目的模块", [team.leader_id])
    publish(
        team.leader,
        foreign_project["id"],
        module_publication([foreign_module]),
    )
    foreign_identity = deepcopy(modules)
    foreign_identity[0]["id"] = foreign_module["id"]
    foreign_identity[1]["upstream"][0]["module_id"] = foreign_module["id"]
    foreign_owner = deepcopy(modules)
    foreign_owner[0]["assignee_ids"] = [outsider_id]
    foreign_dependency = deepcopy(modules)
    foreign_dependency[0]["upstream"] = [{"module_id": foreign_module["id"], "needs": "项目外输入"}]
    path = f"/api/projects/{team.project['id']}/plan"
    for candidate in (foreign_identity, foreign_owner, foreign_dependency):
        assert (
            team.leader.post(
                f"{path}/versions", json=module_publication(candidate), headers=CSRF_HEADERS
            ).status_code
            == 422
        )
    assert team.leader.get(path).json() == first
    assert (
        team.leader.get(
            f"/api/projects/{team.project['id']}/modules/{foreign_module['id']}"
        ).status_code
        == 404
    )


def test_local_module_changes_notify_only_its_old_and_new_owners(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    first = publish_modules(team, modules)
    assert len(notifications(team.member, team.project["id"], first["id"])) == 1
    assert len(notifications(team.other_member, team.project["id"], first["id"])) == 1
    assert notifications(team.leader, team.project["id"], first["id"]) == []

    revised = deepcopy(modules)
    revised[0]["goal"] = "只变更文献综述目标"
    second = publish_modules(team, revised, "只更新一个模块")
    affected = notifications(team.member, team.project["id"], second["id"])
    assert len(affected) == 1
    assert affected[0]["details"]["module_ids"] == [modules[0]["id"]]
    assert notifications(team.other_member, team.project["id"], second["id"]) == []
    assert notifications(team.leader, team.project["id"], second["id"]) == []

    reassigned = deepcopy(revised)
    reassigned[0]["assignee_ids"] = [team.leader_id]
    third = publish_modules(team, reassigned, "变更模块负责人")
    for client in (team.member, team.leader):
        rows = notifications(client, team.project["id"], third["id"])
        assert len(rows) == 1
        assert rows[0]["details"]["module_ids"] == [modules[0]["id"]]
    assert notifications(team.other_member, team.project["id"], third["id"]) == []


def test_overall_plan_changes_notify_all_module_owners_with_one_merged_notice(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    modules[1]["assignee_ids"] = [team.member_id, team.other_member_id]
    first = publish_modules(team, modules)
    initial = notifications(team.member, team.project["id"], first["id"])
    assert len(initial) == 1
    assert set(initial[0]["details"]["module_ids"]) == {item["id"] for item in modules}
    second = publish_modules(team, modules, "更新整体论证", argument="新的整体论证主线")
    for client, user_id, expected_modules in (
        (team.member, team.member_id, {item["id"] for item in modules}),
        (team.other_member, team.other_member_id, {modules[1]["id"]}),
    ):
        rows = notifications(client, team.project["id"], second["id"])
        assert len(rows) == 1
        assert rows[0]["user_id"] == user_id
        assert rows[0]["kind"] == "plan_updated"
        assert rows[0]["details"]["version"] == second["version"]
        assert set(rows[0]["details"]["module_ids"]) == expected_modules
    assert notifications(team.leader, team.project["id"], second["id"]) == []


def test_notifications_are_private_and_read_is_csrf_protected_and_idempotent(auth_environment):
    team = create_team(auth_environment)
    version = publish_modules(team, team_modules(team))
    own = notifications(team.member, team.project["id"], version["id"])[0]
    other = notifications(team.other_member, team.project["id"], version["id"])[0]
    assert own["read_at"] is None
    assert team.member.get("/api/notifications").json() == [own]
    path = f"/api/notifications/{own['id']}/read"
    assert team.member.post(path).status_code == 403
    assert (
        team.member.post(
            path, headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert team.other_member.post(path, headers=CSRF_HEADERS).status_code == 404
    assert (
        team.member.post(f"/api/notifications/{other['id']}/read", headers=CSRF_HEADERS).status_code
        == 404
    )
    anonymous = auth_environment.make_client()
    assert anonymous.get("/api/notifications").status_code == 401
    assert anonymous.post(path, headers=CSRF_HEADERS).status_code == 401
    response = team.member.post(path, headers=CSRF_HEADERS)
    assert response.status_code == 200
    read_notice = response.json()
    assert read_notice["id"] == own["id"]
    assert read_notice["read_at"] is not None
    assert team.member.post(path, headers=CSRF_HEADERS).json() == read_notice
    assert notifications(team.member, team.project["id"]) == [read_notice]
    assert notifications(team.other_member, team.project["id"]) == [other]
    events = team.leader.get(f"/api/projects/{team.project['id']}/events").json()
    read_events = [event for event in events if event["event_type"] == "notification_read"]
    assert len(read_events) == 1
    assert read_events[0]["actor_id"] == team.member_id


def test_members_read_modules_and_task_events_bind_versions(auth_environment):
    team = create_team(auth_environment)
    modules = team_modules(team)
    first = publish_modules(team, modules)
    revised = deepcopy(modules)
    revised[0]["goal"] = "修订后的目标"
    second = publish_modules(team, revised, "更新第一个模块")
    project_path = f"/api/projects/{team.project['id']}"
    assert team.member.get(f"{project_path}/modules").status_code == 200
    assert team.member.get(f"{project_path}/modules/{modules[1]['id']}").status_code == 200
    assert (
        team.member.post(
            f"{project_path}/plan/versions", json=module_publication(modules), headers=CSRF_HEADERS
        ).status_code
        == 403
    )
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    assert outsider.get(f"{project_path}/modules").status_code == 404
    assert outsider.get(f"{project_path}/modules/{modules[0]['id']}").status_code == 404
    assert team.member.get(f"{project_path}/modules/{uuid4()}").status_code == 404
    response = team.member.get(f"{project_path}/events", params={"task_id": modules[0]["id"]})
    assert response.status_code == 200
    events = response.json()
    assert [event["event_type"] for event in events] == ["module_updated", "module_updated"]
    assert [event["plan_version_id"] for event in events] == [first["id"], second["id"]]
    assert all(event["task_id"] == modules[0]["id"] for event in events)
    assert all(event["details"]["removed"] is False for event in events)
    assert [event["details"]["version"] for event in events] == [1, 2]


def test_invalid_module_shapes_do_not_publish_or_create_module_identities(auth_environment):
    team = create_team(auth_environment)
    base = team_modules(team)
    invalid = []
    for field, value in (
        ("name", " "),
        ("goal", ""),
        ("assignee_ids", []),
        ("assignee_ids", [team.member_id, team.member_id]),
        ("deadline", "2026-12-10T18:00:00"),
        ("delivery_format", "exe"),
        ("unexpected", "不允许的字段"),
    ):
        candidate = deepcopy(base)
        candidate[0][field] = value
        invalid.append(candidate)
    duplicate_ids = deepcopy(base)
    duplicate_ids[1]["id"] = duplicate_ids[0]["id"]
    invalid.append(duplicate_ids)
    missing_needs = deepcopy(base)
    missing_needs[1]["upstream"][0]["needs"] = ""
    invalid.append(missing_needs)
    path = f"/api/projects/{team.project['id']}/plan/versions"
    for candidate in invalid:
        assert (
            team.leader.post(
                path, json=module_publication(candidate), headers=CSRF_HEADERS
            ).status_code
            == 422
        )
    assert team.leader.get(path).json() == []
    assert team.leader.get(f"/api/projects/{team.project['id']}/modules").json() == []
    assert notifications(team.member, team.project["id"]) == []
    with auth_environment.sessions() as db:
        assert db.scalars(select(Module)).all() == []
