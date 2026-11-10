import hashlib
from io import BytesIO
from zipfile import ZipFile

from sqlalchemy import select
from test_auth import CSRF_HEADERS, login, register
from test_projects import accept, create_project, invite

from app.models.resource import Resource

BUILTIN_TEMPLATES = {
    "潜学成智-上外PPT模板.pptx": (
        31456440,
        "2fb31f192b48bda2cdda2d17014fa1c46675fe61618907a84c76f65498d0380b",
    ),
    "笃思成章-上外PPT模板.pptx": (
        61780355,
        "15890ec541a0827540b53d167eec9c06688bacff7f1d4fbaa330f4ed5ca794c7",
    ),
}


def upload(client, project_id, content=b"template-test-content", filename="团队模板.md"):
    return client.post(
        f"/api/projects/{project_id}/resources",
        files={"file": (filename, content, "application/octet-stream")},
        headers=CSRF_HEADERS,
    )


def test_presets_include_citation_examples_fonts_handoff_and_two_templates(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    response = leader.get(f"/api/projects/{project['id']}/spec/presets")
    assert response.status_code == 200
    result = response.json()
    presets = {item["key"]: item for item in result["presets"]}
    assert {
        "citation_gbt7714_2015",
        "citation_apa7",
        "fonts_cn_en",
        "handoff",
    } <= set(presets)
    for key in ("citation_gbt7714_2015", "citation_apa7"):
        assert presets[key]["items"]
        for item in presets[key]["items"]:
            assert item["positive_example"].strip()
            assert item["negative_example"].strip()
            assert item["requirement"].strip()
    assert presets["fonts_cn_en"]["items"]
    assert presets["handoff"]["items"]
    assert all(item["category"] == "format" for item in presets["fonts_cn_en"]["items"])
    assert all(item["category"] == "handoff" for item in presets["handoff"]["items"])
    assert {item["filename"] for item in result["templates"]} == set(BUILTIN_TEMPLATES)
    assert all(item["builtin"] is True for item in result["templates"])
    assert {
        item["template_resource_id"]
        for preset in presets.values()
        for item in preset["items"]
        if item["category"] == "template"
    } == {item["id"] for item in result["templates"]}


def test_builtin_pptx_downloads_match_original_bytes_and_are_readable_archives(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    response = leader.get(f"/api/projects/{project['id']}/resources")
    assert response.status_code == 200
    resources = response.json()
    assert len(resources) == 2
    for item in resources:
        expected_size, expected_hash = BUILTIN_TEMPLATES[item["filename"]]
        assert item["builtin"] is True
        assert item["size"] == expected_size
        assert item["sha256"] == expected_hash
        download = leader.get(item["download_url"])
        assert download.status_code == 200
        assert len(download.content) == expected_size
        assert hashlib.sha256(download.content).hexdigest() == expected_hash
        with ZipFile(BytesIO(download.content)) as archive:
            assert "[Content_Types].xml" in archive.namelist()
            assert "ppt/presentation.xml" in archive.namelist()
            assert archive.testzip() is None


def test_leader_upload_is_listed_and_downloaded_unchanged_by_project_members(auth_environment):
    leader = auth_environment.client
    identity = register(leader).json()
    project = create_project(leader)
    content = "# 自定义模板\n结论、来源、下游说明。\n".encode()
    response = upload(leader, project["id"], content)
    assert response.status_code == 201
    resource = response.json()
    assert set(resource) == {"id", "filename", "size", "sha256", "builtin", "download_url"}
    assert resource["filename"] == "团队模板.md"
    assert resource["size"] == len(content)
    assert resource["sha256"] == hashlib.sha256(content).hexdigest()
    assert resource["builtin"] is False
    member = auth_environment.make_client()
    register(member, "member")
    assert accept(member, invite(leader, project["id"])).status_code == 200
    resources = member.get(f"/api/projects/{project['id']}/resources").json()
    assert resource in resources
    assert len(resources) == 3
    assert member.get(resource["download_url"]).content == content
    with auth_environment.sessions() as db:
        rows = db.scalars(select(Resource)).all()
        assert len(rows) == 1
        assert str(rows[0].project_id) == project["id"]
        assert str(rows[0].created_by) == identity["id"]
    events = leader.get(f"/api/projects/{project['id']}/events").json()
    uploads = [event for event in events if event["event_type"] == "template_uploaded"]
    assert len(uploads) == 1
    assert uploads[0]["object_id"] == resource["id"]


def test_resources_and_presets_enforce_membership_and_upload_requires_leader(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    resource = upload(leader, project["id"]).json()
    member = auth_environment.make_client()
    register(member, "member")
    assert accept(member, invite(leader, project["id"])).status_code == 200
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    foreign_project = create_project(outsider)
    anonymous = auth_environment.make_client()
    prefix = f"/api/projects/{project['id']}"
    for path in (f"{prefix}/resources", f"{prefix}/spec/presets", resource["download_url"]):
        assert member.get(path).status_code == 200
        assert outsider.get(path).status_code == 404
        assert anonymous.get(path).status_code == 401
    assert upload(member, project["id"]).status_code == 403
    assert upload(outsider, project["id"]).status_code == 404
    assert upload(anonymous, project["id"]).status_code == 401
    assert (
        outsider.get(
            f"/api/projects/{foreign_project['id']}/resources/{resource['id']}/download"
        ).status_code
        == 404
    )
    assert len(outsider.get(f"/api/projects/{foreign_project['id']}/resources").json()) == 2


def test_resource_upload_limits_and_csrf_fail_without_persisting_resources(auth_environment):
    leader = auth_environment.client
    register(leader)
    project = create_project(leader)
    path = f"/api/projects/{project['id']}/resources"
    files = {"file": ("模板.md", b"1234", "text/markdown")}
    assert leader.post(path, files=files).status_code == 403
    assert (
        leader.post(
            path,
            files=files,
            headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    limited = auth_environment.make_client(upload_max_bytes=4)
    assert login(limited).status_code == 200
    assert upload(limited, project["id"], b"12345").status_code == 413
    assert len(leader.get(path).json()) == 2
    with auth_environment.sessions() as db:
        assert db.scalars(select(Resource)).all() == []
    response = upload(limited, project["id"], b"1234")
    assert response.status_code == 201
    assert response.json()["size"] == 4
    assert limited.get(response.json()["download_url"]).content == b"1234"
