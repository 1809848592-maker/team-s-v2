import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from test_auth import CSRF_HEADERS, login, register
from test_modules import create_team, publish_modules, team_modules
from test_projects import create_project
from test_specs import publish_spec, specification

from app.models import DeliveryVersion, SharedMaterial
from app.models.immutable import SnapshotImmutableError


def ready_team(environment, *, multiple_owners=False):
    team = create_team(environment)
    team.modules = team_modules(team)
    if multiple_owners:
        team.modules[0]["assignee_ids"] = [team.member_id, team.other_member_id]
    team.plan = publish_modules(team, team.modules)
    team.spec = publish_spec(team.leader, team.project["id"])
    return team


def delivery_path(team, module_id=None):
    selected_module = module_id or team.modules[0]["id"]
    return f"/api/projects/{team.project['id']}/modules/{selected_module}/deliveries"


def upload_delivery(
    client,
    team,
    content=b"delivery-content",
    *,
    module_id=None,
    filename="研究交付.md",
    ai_participation="assisted",
    ai_note="AI 辅助整理结构，作者核对内容。",
    handoff_note="结论、来源和给下游的说明。",
    headers=None,
):
    return client.post(
        delivery_path(team, module_id),
        files={"file": (filename, content, "application/octet-stream")},
        data={
            "ai_participation": ai_participation,
            "ai_note": ai_note,
            "handoff_note": handoff_note,
        },
        headers=CSRF_HEADERS if headers is None else headers,
    )


def upload_material(client, project_id, content=b"shared-content", filename="共享材料.md"):
    return client.post(
        f"/api/projects/{project_id}/materials",
        files={"file": (filename, content, "application/octet-stream")},
        headers=CSRF_HEADERS,
    )


def test_formal_upload_needs_published_spec_and_current_module_owner(auth_environment):
    team = create_team(auth_environment)
    team.modules = team_modules(team)
    team.plan = publish_modules(team, team.modules)
    assert upload_delivery(team.member, team).status_code == 422
    assert team.member.get(delivery_path(team)).json() == []
    team.spec = publish_spec(team.leader, team.project["id"])
    assert upload_delivery(team.leader, team).status_code == 403
    assert upload_delivery(team.other_member, team).status_code == 403
    response = upload_delivery(team.member, team)
    assert response.status_code == 201
    assert response.json()["version"] == 1

    revised = deepcopy(team.modules)
    revised[0]["assignee_ids"] = [team.other_member_id]
    publish_modules(team, revised, "变更模块负责人")
    assert upload_delivery(team.member, team).status_code == 403
    response = upload_delivery(team.other_member, team)
    assert response.status_code == 201
    assert response.json()["version"] == 2


def test_old_versions_download_original_bytes_and_content_hashes_deduplicate_storage(
    auth_environment,
):
    team = ready_team(auth_environment)
    versions = []
    first_content, second_content = b"original-version-content", b"revised-version-content"
    for content in (first_content, second_content, first_content):
        response = upload_delivery(team.member, team, content)
        assert response.status_code == 201
        versions.append(response.json())
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert len({item["id"] for item in versions}) == 3
    assert (
        versions[0]["sha256"] == versions[2]["sha256"] == hashlib.sha256(first_content).hexdigest()
    )
    assert versions[1]["sha256"] == hashlib.sha256(second_content).hexdigest()
    for version, content in zip(
        versions, (first_content, second_content, first_content), strict=True
    ):
        response = team.member.get(version["download_url"])
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-disposition"].startswith("attachment")
        assert version["size"] == len(content)
    listed = team.leader.get(delivery_path(team)).json()
    assert {item["id"] for item in listed} == {item["id"] for item in versions}
    storage_root = team.leader.app.state.settings.storage_dir
    stored_files = [path for path in storage_root.rglob("*") if path.is_file()]
    assert len(stored_files) == 2
    assert {path.name for path in stored_files} == {item["sha256"] for item in versions}
    with auth_environment.sessions() as db:
        assert len(db.scalars(select(DeliveryVersion)).all()) == 3


def test_concurrent_submitters_share_one_contiguous_module_version_sequence(auth_environment):
    team = ready_team(auth_environment, multiple_owners=True)
    barrier = Barrier(2)

    def submit(client, content):
        barrier.wait(timeout=5)
        return upload_delivery(client, team, content)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(submit, team.member, b"first-owner-content"),
            executor.submit(submit, team.other_member, b"second-owner-content"),
        ]
        responses = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    versions = [response.json() for response in responses]
    assert sorted(item["version"] for item in versions) == [1, 2]
    assert len({item["id"] for item in versions}) == 2
    assert {item["submitted_by"] for item in versions} == {team.member_id, team.other_member_id}
    second_module = upload_delivery(
        team.other_member, team, b"another-module-content", module_id=team.modules[1]["id"]
    )
    assert second_module.status_code == 201
    assert second_module.json()["version"] == 1


def test_submitted_deliveries_are_visible_to_all_project_people_but_isolated_by_project_and_module(
    auth_environment,
):
    team = ready_team(auth_environment)
    response = upload_delivery(team.member, team)
    assert response.status_code == 201
    version = response.json()
    all_path = f"/api/projects/{team.project['id']}/deliveries"
    outsider = auth_environment.make_client()
    outsider_id = register(outsider, "outsider").json()["id"]
    foreign_project = create_project(outsider)
    admin = auth_environment.make_client(platform_admin_user_ids=outsider_id)
    assert login(admin, "outsider").json()["is_admin"] is True
    anonymous = auth_environment.make_client()
    for client in (team.leader, team.member, team.other_member):
        assert client.get(delivery_path(team)).json() == [version]
        assert client.get(all_path).json() == [version]
        assert client.get(version["download_url"]).content == b"delivery-content"
    for client, code in ((outsider, 404), (admin, 404), (anonymous, 401)):
        assert client.get(all_path).status_code == code
        assert client.get(delivery_path(team)).status_code == code
        assert client.get(version["download_url"]).status_code == code
        assert upload_delivery(client, team).status_code == code
    assert team.member.get(delivery_path(team, str(uuid4()))).status_code == 404
    assert upload_delivery(team.member, team, module_id=str(uuid4())).status_code == 404
    wrong_module_path = f"{delivery_path(team, team.modules[1]['id'])}/{version['id']}/download"
    assert team.member.get(wrong_module_path).status_code == 404
    assert (
        outsider.get(
            f"/api/projects/{foreign_project['id']}/modules/{team.modules[0]['id']}"
            f"/deliveries/{version['id']}/download"
        ).status_code
        == 404
    )


def test_shared_materials_need_no_plan_or_spec_and_never_create_formal_versions(auth_environment):
    team = create_team(auth_environment)
    path = f"/api/projects/{team.project['id']}"
    response = upload_material(team.member, team.project["id"], b"shared-source")
    assert response.status_code == 201
    material = response.json()
    assert material["uploaded_by"] == team.member_id
    assert material["sha256"] == hashlib.sha256(b"shared-source").hexdigest()
    assert "status" not in material
    assert team.member.get(f"{path}/materials").json() == [material]
    assert team.member.get(f"{path}/deliveries").json() == []
    for client in (team.leader, team.member, team.other_member):
        assert client.get(material["download_url"]).content == b"shared-source"
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    assert outsider.get(f"{path}/materials").status_code == 404
    assert outsider.get(material["download_url"]).status_code == 404
    assert upload_material(outsider, team.project["id"]).status_code == 404
    anonymous = auth_environment.make_client()
    assert anonymous.get(f"{path}/materials").status_code == 401
    assert anonymous.get(material["download_url"]).status_code == 401
    assert upload_material(anonymous, team.project["id"]).status_code == 401
    with auth_environment.sessions() as db:
        assert len(db.scalars(select(SharedMaterial)).all()) == 1
        assert db.scalars(select(DeliveryVersion)).all() == []
    events = team.leader.get(f"{path}/events").json()
    material_events = [event for event in events if event["event_type"] == "material_uploaded"]
    assert len(material_events) == 1
    assert material_events[0]["actor_id"] == team.member_id
    assert material_events[0]["object_id"] == material["id"]
    assert material_events[0]["plan_version_id"] is None
    assert material_events[0]["spec_version_id"] is None
    assert material_events[0]["task_id"] is None

    team.modules = team_modules(team)
    team.plan = publish_modules(team, team.modules)
    team.spec = publish_spec(team.leader, team.project["id"])
    response = upload_delivery(team.member, team, b"shared-source")
    assert response.status_code == 201
    formal = response.json()
    assert formal["id"] != material["id"]
    assert formal["version"] == 1
    assert team.member.get(f"{path}/materials").json() == [material]
    assert team.member.get(f"{path}/deliveries").json() == [formal]
    assert team.member.get(f"{path}/materials/{formal['id']}/download").status_code == 404
    assert team.member.get(f"{delivery_path(team)}/{material['id']}/download").status_code == 404


def test_upload_limits_filenames_declarations_and_csrf_fail_without_creating_versions(
    auth_environment,
):
    team = ready_team(auth_environment)
    path = delivery_path(team)
    assert upload_delivery(team.member, team, headers={}).status_code == 403
    assert (
        upload_delivery(
            team.member,
            team,
            headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert upload_delivery(team.member, team, filename="..").status_code == 422
    assert upload_delivery(team.member, team, ai_note="x" * 2001).status_code == 422
    assert upload_delivery(team.member, team, handoff_note="x" * 10001).status_code == 422
    assert upload_delivery(team.member, team, ai_participation="unknown").status_code == 422
    assert (
        team.member.post(
            path,
            files={"file": ("缺少声明.md", b"content", "text/markdown")},
            headers=CSRF_HEADERS,
        ).status_code
        == 422
    )
    limited = auth_environment.make_client(upload_max_bytes=4)
    assert login(limited, "member").status_code == 200
    assert upload_delivery(limited, team, b"12345").status_code == 413
    assert upload_material(limited, team.project["id"], b"12345").status_code == 413
    assert team.member.get(path).json() == []
    assert team.member.get(f"/api/projects/{team.project['id']}/materials").json() == []
    material_path = f"/api/projects/{team.project['id']}/materials"
    assert (
        team.member.post(
            material_path, files={"file": ("共享.md", b"1234", "text/markdown")}
        ).status_code
        == 403
    )
    assert (
        team.member.post(
            material_path,
            files={"file": ("共享.md", b"1234", "text/markdown")},
            headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    response = upload_delivery(limited, team, b"1234", filename="../交付.md")
    assert response.status_code == 201
    assert response.json()["filename"] == "交付.md"
    assert response.json()["version"] == 1
    assert response.json()["size"] == 4
    material = upload_material(limited, team.project["id"], b"1234")
    assert material.status_code == 201
    assert limited.get(material.json()["download_url"]).content == b"1234"


def test_delivery_snapshots_cannot_be_rewritten_through_orm_database_or_http(auth_environment):
    team = ready_team(auth_environment)
    response = upload_delivery(team.member, team)
    assert response.status_code == 201
    version = response.json()
    version_id = UUID(version["id"])
    with auth_environment.sessions() as db:
        snapshot = db.get(DeliveryVersion, version_id)
        snapshot.filename = "改写历史.md"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        snapshot = db.get(DeliveryVersion, version_id)
        with pytest.raises(SnapshotImmutableError):
            db.delete(snapshot)
            db.flush()
        db.rollback()
        for statement in (
            update(DeliveryVersion)
            .where(DeliveryVersion.id == version_id)
            .values(filename="改写历史.md"),
            delete(DeliveryVersion).where(DeliveryVersion.id == version_id),
        ):
            with pytest.raises(SnapshotImmutableError):
                db.execute(statement)
            db.rollback()
        for statement in (
            update(DeliveryVersion.__table__)
            .where(DeliveryVersion.id == version_id)
            .values(filename="改写历史.md"),
            delete(DeliveryVersion.__table__).where(DeliveryVersion.id == version_id),
        ):
            with pytest.raises(DBAPIError):
                db.connection().execute(statement)
            db.rollback()
    for method in ("PUT", "PATCH", "DELETE"):
        assert team.member.request(
            method, f"{delivery_path(team)}/{version['id']}", json={}, headers=CSRF_HEADERS
        ).status_code in (404, 405)
    assert team.member.get(delivery_path(team)).json() == [version]
    assert team.member.get(version["download_url"]).content == b"delivery-content"
    material_response = upload_material(team.member, team.project["id"])
    assert material_response.status_code == 201
    material = material_response.json()
    material_id = UUID(material["id"])
    with auth_environment.sessions() as db:
        snapshot = db.get(SharedMaterial, material_id)
        snapshot.filename = "改写共享材料.md"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        with pytest.raises(DBAPIError):
            db.connection().execute(
                delete(SharedMaterial.__table__).where(SharedMaterial.id == material_id)
            )
        db.rollback()
    assert team.member.get(material["download_url"]).content == b"shared-content"


def test_submission_records_human_declaration_handoff_and_exact_version_event(auth_environment):
    team = ready_team(auth_environment)
    note, handoff = "AI 主要起草，提交人已逐项验证。", "结论与来源已核对，给下游保留待办。"
    response = upload_delivery(
        team.member, team, ai_participation="primary", ai_note=note, handoff_note=handoff
    )
    assert response.status_code == 201
    version = response.json()
    assert version["status"] == "submitted"
    assert version["submitted_by"] == team.member_id
    assert version["module_id"] == team.modules[0]["id"]
    assert version["plan_version_id"] == team.plan["id"]
    assert version["spec_version_id"] == team.spec["id"]
    assert version["ai_participation"] == "primary"
    assert version["ai_note"] == note
    assert version["handoff_note"] == handoff
    with auth_environment.sessions() as db:
        stored = db.get(DeliveryVersion, UUID(version["id"]))
        assert stored.ai_participation == "primary"
        assert stored.ai_note == note
        assert stored.handoff_note == handoff
    response = team.leader.get(
        f"/api/projects/{team.project['id']}/events", params={"task_id": team.modules[0]["id"]}
    )
    events = [event for event in response.json() if event["event_type"] == "delivery_submitted"]
    assert len(events) == 1
    event = events[0]
    assert event["actor_type"] == "human"
    assert event["actor_id"] == team.member_id
    assert event["object_id"] == version["id"]
    assert event["plan_version_id"] == team.plan["id"]
    assert event["spec_version_id"] == team.spec["id"]
    assert event["details"]["ai_participation"] == "primary"
    assert event["details"]["ai_note"] == note
    assert event["details"]["handoff_note"] == handoff


def test_new_submissions_bind_latest_plan_and_spec_without_changing_old_versions(auth_environment):
    team = ready_team(auth_environment)
    response = upload_delivery(team.member, team, b"first-submission")
    assert response.status_code == 201
    first = response.json()
    revised_modules = deepcopy(team.modules)
    revised_modules[0]["goal"] = "更新后的模块问题"
    new_plan = publish_modules(team, revised_modules, "发布新方案")
    new_spec = publish_spec(team.leader, team.project["id"], specification("发布新规范"))
    response = upload_delivery(team.member, team, b"second-submission")
    assert response.status_code == 201
    second = response.json()
    assert second["version"] == 2
    assert second["plan_version_id"] == new_plan["id"] != first["plan_version_id"]
    assert second["spec_version_id"] == new_spec["id"] != first["spec_version_id"]
    versions = team.member.get(delivery_path(team)).json()
    historical = next(item for item in versions if item["id"] == first["id"])
    # 交付快照不变；动态绑定视图随项目发布更新，不属于原文件快照。
    assert {key: value for key, value in historical.items() if key != "version_binding"} == {
        key: value for key, value in first.items() if key != "version_binding"
    }
    assert historical["version_binding"]["plan_outdated"]
    assert historical["version_binding"]["spec_outdated"]
    assert first["plan_version_id"] == team.plan["id"]
    assert first["spec_version_id"] == team.spec["id"]
    assert team.member.get(first["download_url"]).content == b"first-submission"
    removed_plan = publish_modules(team, [], "移除当前分工模块")
    archived = team.member.get(delivery_path(team)).json()
    assert [
        {key: value for key, value in item.items() if key != "version_binding"} for item in archived
    ] == [
        {key: value for key, value in item.items() if key != "version_binding"} for item in versions
    ]
    assert all(
        item["version_binding"]["latest_plan_version_id"] == removed_plan["id"] for item in archived
    )
    assert team.member.get(first["download_url"]).content == b"first-submission"
    assert upload_delivery(team.member, team).status_code == 404
