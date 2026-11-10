from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError
from test_auth import CSRF_HEADERS, login, register
from test_projects import accept, create_project, invite

from app.models import PlanVersion
from app.models.immutable import SnapshotImmutableError


def publication(change_note="发布整体研究方案"):
    return {
        "change_note": change_note,
        "content": {
            "research_question": "人机混合科研团队如何保留交接信息？",
            "final_deliverable": "研究论文与汇报 PPT",
            "deadline": "2026-12-20T18:00:00+08:00",
            "argument": "任务结构与交付审核共同影响信息完整性。",
            "terms": [
                {"term": "交接", "definition": "任务成果及依据的传递", "source": "研究方案"},
                {"term": "负责人", "definition": "承担组织与终审责任的人", "source": "团队规范"},
                {"term": "交付", "definition": "供团队审核采用的成果版本", "source": "研究方案"},
            ],
            "data_definitions": [
                {"variable": "交接次数", "source": "项目事件日志", "processing": "按任务统计"},
                {"variable": "退回轮次", "source": "审核记录", "processing": "按交付统计"},
            ],
            "modules": [],
        },
    }


def publish(client, project_id, payload=None):
    response = client.post(
        f"/api/projects/{project_id}/plan/versions",
        json=payload or publication(),
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201
    return response.json()


def test_structured_plan_roundtrip_and_unpublished_project(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader)
    path = f"/api/projects/{project['id']}/plan"
    assert leader.get(path).status_code == 404
    assert leader.get(f"{path}/versions").json() == []

    payload = publication()
    version = publish(leader, project["id"], payload)
    assert set(version) == {
        "id",
        "project_id",
        "version",
        "change_note",
        "content",
        "published_by",
        "published_at",
    }
    assert version["project_id"] == project["id"]
    assert version["version"] == 1
    assert version["published_by"] == identity["id"]
    assert version["change_note"] == payload["change_note"]
    assert datetime.fromisoformat(version["published_at"]).tzinfo is not None
    content = version["content"]
    assert content | {"deadline": payload["content"]["deadline"]} == payload["content"]
    parsed_deadline = datetime.fromisoformat(content["deadline"])
    assert parsed_deadline == datetime(2026, 12, 20, 10, tzinfo=UTC)
    assert parsed_deadline.tzinfo == UTC
    assert leader.get(path).json() == version
    assert leader.get(f"{path}/versions/{version['id']}").json() == version
    assert leader.get(f"{path}/versions").json() == [version]


def test_publishing_increments_version_and_preserves_history(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    first = publish(leader, project["id"])
    payload = publication("补充论证与交付要求")
    payload["content"]["argument"] = "修订后的论证主线。"
    payload["content"]["terms"][0]["definition"] = "成果、依据与上下游约束的传递"
    second = publish(leader, project["id"], payload)
    path = f"/api/projects/{project['id']}/plan"

    assert [first["version"], second["version"]] == [1, 2]
    assert first["id"] != second["id"]
    assert leader.get(path).json() == second
    assert leader.get(f"{path}/versions").json() == [second, first]
    assert leader.get(f"{path}/versions/{first['id']}").json() == first
    assert second["content"]["argument"] == payload["content"]["argument"]
    assert first["content"]["argument"] != second["content"]["argument"]


def test_published_plan_is_read_only_through_orm_database_and_http(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    version = publish(leader, project["id"])
    version_id = UUID(version["id"])
    with auth_environment.sessions() as db:
        snapshot = db.get(PlanVersion, version_id)
        snapshot.change_note = "伪造历史"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        snapshot = db.get(PlanVersion, version_id)
        with pytest.raises(SnapshotImmutableError):
            db.delete(snapshot)
            db.flush()
        db.rollback()
        for statement in (
            update(PlanVersion).where(PlanVersion.id == version_id).values(change_note="伪造历史"),
            delete(PlanVersion).where(PlanVersion.id == version_id),
        ):
            with pytest.raises(SnapshotImmutableError):
                db.execute(statement)
            db.rollback()
        for statement in (
            update(PlanVersion.__table__)
            .where(PlanVersion.id == version_id)
            .values(change_note="伪造历史"),
            delete(PlanVersion.__table__).where(PlanVersion.id == version_id),
        ):
            with pytest.raises(DBAPIError):
                db.connection().execute(statement)
            db.rollback()

    path = f"/api/projects/{project['id']}/plan"
    for target in (path, f"{path}/versions/{version['id']}"):
        for method in ("PUT", "PATCH", "DELETE"):
            assert leader.request(
                method, target, json=publication(), headers=CSRF_HEADERS
            ).status_code in (404, 405)
    assert leader.get(f"{path}/versions/{version['id']}").json() == version


def test_only_leaders_publish_and_members_cannot_read_other_projects_versions(auth_environment):
    leader = auth_environment.client
    register(leader, "leader")
    project = create_project(leader)
    version = publish(leader, project["id"])
    member = auth_environment.make_client()
    register(member, "member")
    assert accept(member, invite(leader, project["id"])).status_code == 200
    outsider = auth_environment.make_client()
    outsider_identity = register(outsider, "outsider").json()
    other_project = create_project(outsider)
    other_version = publish(outsider, other_project["id"])
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_identity["id"])
    assert login(admin, "outsider").json()["is_admin"] is True
    anonymous = auth_environment.make_client()
    path = f"/api/projects/{project['id']}/plan"

    for target in (path, f"{path}/versions", f"{path}/versions/{version['id']}"):
        assert member.get(target).status_code == 200
        assert outsider.get(target).status_code == 404
        assert admin.get(target).status_code == 404
        assert anonymous.get(target).status_code == 401
    for client, status_code in ((member, 403), (outsider, 404), (admin, 404), (anonymous, 401)):
        assert (
            client.post(f"{path}/versions", json=publication(), headers=CSRF_HEADERS).status_code
            == status_code
        )
    assert leader.post(f"{path}/versions", json=publication()).status_code == 403
    assert (
        leader.post(
            f"{path}/versions",
            json=publication(),
            headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert leader.get(f"{path}/versions/{other_version['id']}").status_code == 404
    assert member.get(f"{path}/versions/{other_version['id']}").status_code == 404
    assert leader.get(f"{path}/versions/{uuid4()}").status_code == 404
    assert leader.get(f"{path}/versions").json() == [version]


def test_concurrent_leader_publications_allocate_distinct_contiguous_versions(auth_environment):
    leader = auth_environment.client
    leader_identity = register(leader, "leader").json()
    project = create_project(leader)
    second_leader = auth_environment.make_client()
    second_identity = register(second_leader, "second-leader").json()
    assert accept(second_leader, invite(leader, project["id"], "leader")).status_code == 200
    barrier = Barrier(2)

    def concurrent_publish(client, note):
        barrier.wait(timeout=5)
        return publish(client, project["id"], publication(note))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(concurrent_publish, leader, "第一位负责人发布"),
            executor.submit(concurrent_publish, second_leader, "第二位负责人发布"),
        ]
        versions = [future.result(timeout=10) for future in futures]
    assert sorted(version["version"] for version in versions) == [1, 2]
    assert len({version["id"] for version in versions}) == 2
    ordered = leader.get(f"/api/projects/{project['id']}/plan/versions").json()
    assert [version["version"] for version in ordered] == [2, 1]
    assert {version["change_note"] for version in ordered} == {
        "第一位负责人发布",
        "第二位负责人发布",
    }
    assert {version["change_note"]: version["published_by"] for version in ordered} == {
        "第一位负责人发布": leader_identity["id"],
        "第二位负责人发布": second_identity["id"],
    }


def test_publication_events_reference_the_exact_immutable_version(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader)
    versions = [
        publish(leader, project["id"], publication("初次发布")),
        publish(leader, project["id"], publication("修订后发布")),
    ]
    events = leader.get(f"/api/projects/{project['id']}/events").json()
    publications = [event for event in events if event["event_type"] == "plan_published"]
    assert len(publications) == 2
    for event, version in zip(publications, versions, strict=True):
        assert event["project_id"] == project["id"]
        assert event["actor_id"] == identity["id"] == version["published_by"]
        assert event["actor_type"] == "human"
        assert event["object_type"] == "plan_version"
        assert event["object_id"] == event["plan_version_id"] == version["id"]
        assert event["details"]["version"] == version["version"]
        assert event["details"]["change_note"] == version["change_note"]


def test_plan_validation_rejects_invalid_structures_and_keeps_version_sequence(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    path = f"/api/projects/{project['id']}/plan/versions"
    invalid = []
    for field, value in (
        ("research_question", "   "),
        ("final_deliverable", ""),
        ("deadline", "2026-12-20T18:00:00"),
        ("deadline", "not-a-date"),
        ("terms", "not-a-list"),
        ("terms", [{"term": "定义缺失"}]),
        ("data_definitions", [{"source": "缺少变量名"}]),
        ("unexpected", "不允许的字段"),
    ):
        payload = publication()
        payload["content"][field] = value
        invalid.append(payload)
    payload = publication("")
    invalid.append(payload)
    payload = publication()
    payload["published_by"] = str(uuid4())
    invalid.append(payload)
    payload = publication()
    payload["content"]["terms"][0]["unexpected"] = "不允许的条目字段"
    invalid.append(payload)
    for payload in invalid:
        assert leader.post(path, json=payload, headers=CSRF_HEADERS).status_code == 422
    assert leader.get(path).json() == []

    minimal = {
        "change_note": "发布最小方案",
        "content": {"research_question": "研究问题", "final_deliverable": "论文"},
    }
    version = publish(leader, project["id"], deepcopy(minimal))
    assert version["version"] == 1
    assert version["content"] == minimal["content"] | {
        "argument": "",
        "deadline": None,
        "terms": [],
        "data_definitions": [],
        "modules": [],
    }
