from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from test_auth import CSRF_HEADERS, login, register
from test_projects import accept, create_project, invite
from test_resources import upload

from app.models import SpecVersion
from app.models.immutable import SnapshotImmutableError


def specification(note="发布团队规范"):
    return {
        "change_note": note,
        "items": [
            {
                "number": "W1",
                "category": "writing",
                "requirement": "关键术语与项目方案保持一致。",
                "check_method": "ai",
                "positive_example": "使用方案给定的术语定义。",
                "negative_example": "为同一术语擅自更换定义。",
                "template_resource_id": None,
            },
            {
                "number": "T1",
                "category": "template",
                "requirement": "汇报使用选定的团队模板。",
                "check_method": "manual",
                "positive_example": "在团队模板上制作汇报。",
                "negative_example": "使用无关的汇报模板。",
                "template_resource_id": None,
            },
            {
                "number": "F1",
                "category": "format",
                "requirement": "中文正文使用宋体、小四号。",
                "check_method": "program",
                "positive_example": "宋体、小四号正文。",
                "negative_example": "任意混用正文字体字号。",
                "template_resource_id": None,
            },
            {
                "number": "H1",
                "category": "handoff",
                "requirement": "交接说明包含结论、数据来源及对下游的说明。",
                "check_method": "ai",
                "positive_example": "逐项列出结论、来源和下游待办。",
                "negative_example": "仅上传文件，没有交接说明。",
                "template_resource_id": None,
            },
        ],
    }


def publish_spec(client, project_id, payload=None):
    response = client.post(
        f"/api/projects/{project_id}/spec/versions",
        json=payload or specification(),
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def test_four_spec_categories_and_edited_presets_publish_as_structured_items(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader)
    path = f"/api/projects/{project['id']}/spec"
    assert leader.get(path).status_code == 404
    assert leader.get(f"{path}/versions").json() == []
    presets = leader.get(f"{path}/presets").json()
    payload = specification()
    payload["items"][1]["template_resource_id"] = presets["templates"][0]["id"]
    citation = deepcopy(
        next(item for item in presets["presets"] if item["key"] == "citation_gbt7714_2015")[
            "items"
        ][0]
    )
    citation["number"] = "R1"
    citation["requirement"] += " 本项目统一核对参考文献条目信息。"
    payload["items"].append(citation)

    version = publish_spec(leader, project["id"], payload)
    assert set(version) == {
        "id",
        "project_id",
        "version",
        "change_note",
        "items",
        "published_by",
        "published_at",
    }
    assert version["version"] == 1
    assert version["project_id"] == project["id"]
    assert version["published_by"] == identity["id"]
    assert version["items"] == payload["items"]
    assert {item["category"] for item in version["items"]} == {
        "writing",
        "template",
        "format",
        "handoff",
    }
    assert {item["check_method"] for item in version["items"]} == {"program", "ai", "manual"}
    assert leader.get(path).json() == version


def test_spec_versions_increment_and_history_stays_immutable(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    first = publish_spec(leader, project["id"])
    payload = specification("调整字体规范")
    payload["items"][2]["requirement"] = "中文正文统一使用仿宋、小四号。"
    second = publish_spec(leader, project["id"], payload)
    path = f"/api/projects/{project['id']}/spec"
    assert [first["version"], second["version"]] == [1, 2]
    assert leader.get(path).json() == second
    assert leader.get(f"{path}/versions").json() == [second, first]
    assert leader.get(f"{path}/versions/{first['id']}").json() == first
    version_id = UUID(first["id"])
    with auth_environment.sessions() as db:
        snapshot = db.get(SpecVersion, version_id)
        snapshot.items = []
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        snapshot = db.get(SpecVersion, version_id)
        with pytest.raises(SnapshotImmutableError):
            db.delete(snapshot)
            db.flush()
        db.rollback()
        for statement in (
            update(SpecVersion).where(SpecVersion.id == version_id).values(change_note="伪造历史"),
            delete(SpecVersion).where(SpecVersion.id == version_id),
        ):
            with pytest.raises(SnapshotImmutableError):
                db.execute(statement)
            db.rollback()
        for statement in (
            update(SpecVersion.__table__)
            .where(SpecVersion.id == version_id)
            .values(change_note="伪造历史"),
            delete(SpecVersion.__table__).where(SpecVersion.id == version_id),
        ):
            with pytest.raises(DBAPIError):
                db.connection().execute(statement)
            db.rollback()
    for method in ("PUT", "PATCH", "DELETE"):
        assert leader.request(
            method, f"{path}/versions/{first['id']}", json=payload, headers=CSRF_HEADERS
        ).status_code in (404, 405)
    assert leader.get(f"{path}/versions/{first['id']}").json() == first


def test_spec_read_and_publish_permissions_cannot_be_bypassed_with_foreign_version_ids(
    auth_environment,
):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    version = publish_spec(leader, project["id"])
    member = auth_environment.make_client()
    register(member, "member")
    assert accept(member, invite(leader, project["id"])).status_code == 200
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    foreign_project = create_project(outsider)
    foreign_version = publish_spec(outsider, foreign_project["id"])
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_id)
    assert login(admin, "outsider").json()["is_admin"] is True
    anonymous = auth_environment.make_client()
    path = f"/api/projects/{project['id']}/spec"
    for target in (path, f"{path}/versions", f"{path}/versions/{version['id']}"):
        assert member.get(target).status_code == 200
        assert outsider.get(target).status_code == 404
        assert admin.get(target).status_code == 404
        assert anonymous.get(target).status_code == 401
    for client, status_code in ((member, 403), (outsider, 404), (admin, 404), (anonymous, 401)):
        assert (
            client.post(f"{path}/versions", json=specification(), headers=CSRF_HEADERS).status_code
            == status_code
        )
    assert leader.post(f"{path}/versions", json=specification()).status_code == 403
    assert (
        leader.post(
            f"{path}/versions",
            json=specification(),
            headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert member.get(f"{path}/versions/{foreign_version['id']}").status_code == 404
    assert leader.get(f"{path}/versions/{foreign_version['id']}").status_code == 404
    assert leader.get(f"{path}/versions").json() == [version]


def test_concurrent_spec_publications_allocate_distinct_contiguous_versions(auth_environment):
    leader = auth_environment.client
    leader_id = register(leader, "leader").json()["id"]
    project = create_project(leader)
    second_leader = auth_environment.make_client()
    second_id = register(second_leader, "second-leader").json()["id"]
    assert accept(second_leader, invite(leader, project["id"], "leader")).status_code == 200
    barrier = Barrier(2)

    def concurrently_publish(client, note):
        barrier.wait(timeout=5)
        return publish_spec(client, project["id"], specification(note))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(concurrently_publish, leader, "第一位负责人发布"),
            executor.submit(concurrently_publish, second_leader, "第二位负责人发布"),
        ]
        versions = [future.result(timeout=10) for future in futures]
    assert sorted(item["version"] for item in versions) == [1, 2]
    assert len({item["id"] for item in versions}) == 2
    assert {item["change_note"]: item["published_by"] for item in versions} == {
        "第一位负责人发布": leader_id,
        "第二位负责人发布": second_id,
    }


def test_invalid_spec_shapes_and_duplicate_numbers_do_not_consume_versions(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    invalid = []
    for field, value in (
        ("number", " "),
        ("requirement", ""),
        ("category", "unknown"),
        ("check_method", "automatic"),
        ("positive_example", None),
        ("template_resource_id", "not-a-uuid"),
        ("unexpected", "不允许的条目字段"),
    ):
        payload = specification()
        payload["items"][0][field] = value
        invalid.append(payload)
    payload = specification()
    payload["items"][1]["number"] = payload["items"][0]["number"]
    invalid.append(payload)
    invalid.append({"change_note": "空规范", "items": []})
    payload = specification()
    payload["published_by"] = str(uuid4())
    invalid.append(payload)
    path = f"/api/projects/{project['id']}/spec/versions"
    for payload in invalid:
        assert leader.post(path, json=payload, headers=CSRF_HEADERS).status_code == 422
    assert leader.get(path).json() == []
    minimal = {
        "change_note": "发布最小规范",
        "items": [
            {
                "number": "A1",
                "category": "writing",
                "requirement": "采用统一术语。",
                "check_method": "ai",
            }
        ],
    }
    version = publish_spec(leader, project["id"], minimal)
    assert version["version"] == 1
    assert version["items"][0] == minimal["items"][0] | {
        "positive_example": "",
        "negative_example": "",
        "template_resource_id": None,
    }


def test_template_references_must_resolve_inside_the_publishing_project(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    own_resource = upload(leader, project["id"]).json()
    foreign_project = create_project(leader, "另一个项目")
    foreign_resource = upload(leader, foreign_project["id"]).json()
    path = f"/api/projects/{project['id']}/spec/versions"
    for resource_id in (foreign_resource["id"], str(uuid4())):
        payload = specification()
        payload["items"][1]["template_resource_id"] = resource_id
        assert leader.post(path, json=payload, headers=CSRF_HEADERS).status_code == 422
    assert leader.get(path).json() == []
    payload = specification()
    payload["items"][1]["template_resource_id"] = own_resource["id"]
    version = publish_spec(leader, project["id"], payload)
    assert version["version"] == 1
    assert version["items"][1]["template_resource_id"] == own_resource["id"]


def test_spec_events_bind_exact_versions_and_spec_only_notifications_can_be_read(auth_environment):
    leader = auth_environment.client
    leader_id = register(leader, "leader").json()["id"]
    project = create_project(leader)
    member = auth_environment.make_client()
    member_id = register(member, "member").json()["id"]
    assert accept(member, invite(leader, project["id"])).status_code == 200
    versions = [
        publish_spec(leader, project["id"], specification("首次规范")),
        publish_spec(leader, project["id"], specification("修订规范")),
    ]
    events = leader.get(f"/api/projects/{project['id']}/events").json()
    publications = [event for event in events if event["event_type"] == "spec_published"]
    assert len(publications) == 2
    for event, version in zip(publications, versions, strict=True):
        assert event["spec_version_id"] == version["id"]
        assert event["plan_version_id"] is None
        assert event["actor_id"] == leader_id
        assert event["details"]["version"] == version["version"]
    for client, user_id in ((leader, leader_id), (member, member_id)):
        rows = client.get("/api/notifications", params={"project_id": project["id"]}).json()
        assert len(rows) == 2
        assert all(row["user_id"] == user_id and row["kind"] == "spec_updated" for row in rows)
        assert {row["details"]["spec_version_id"] for row in rows} == {
            version["id"] for version in versions
        }
        own = rows[0]
        assert "plan_version_id" not in own["details"]
        response = client.post(f"/api/notifications/{own['id']}/read", headers=CSRF_HEADERS)
        assert response.status_code == 200
        assert response.json()["read_at"] is not None
        assert (
            client.post(f"/api/notifications/{own['id']}/read", headers=CSRF_HEADERS).json()
            == response.json()
        )
    read_events = [
        event
        for event in leader.get(f"/api/projects/{project['id']}/events").json()
        if event["event_type"] == "notification_read"
    ]
    assert len(read_events) == 2
    assert all(event["plan_version_id"] is None for event in read_events)
    assert {event["spec_version_id"] for event in read_events} == {versions[1]["id"]}
