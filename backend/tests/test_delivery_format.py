"""真实PG正式交付：固定版本规范、原文件完整性、无读权限扩张及状态副作用。"""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_format_rules import item, word_bytes
from test_specs import publish_spec
from test_text_extraction import snapshot, storage_for

from app.review.extract_types import ExtractionLimits
from app.services.format_check import FormatCheckError, inspect_delivery_format


def inspect(environment, team, delivery, **kwargs):
    return inspect_delivery_format(
        environment.sessions,
        project_id=UUID(team.project["id"]),
        delivery_id=UUID(delivery["id"]),
        storage=kwargs.pop("storage", storage_for(environment)),
        **kwargs,
    )


def test_formal_old_delivery_always_checks_bound_spec_and_bytes_with_no_writes(auth_environment):
    team = ready_team(auth_environment)
    old_spec = publish_spec(
        team.leader,
        team.project["id"],
        {
            "change_note": "固定旧版要求",
            "items": [item("英文正文使用Times New Roman12磅。")],
        },
    )
    first = upload_delivery(team.member, team, word_bytes(), filename="fixed.docx").json()
    new_spec = publish_spec(
        team.leader,
        team.project["id"],
        {
            "change_note": "新版要求",
            "items": [item("英文正文使用Arial12磅。")],
        },
    )
    second = upload_delivery(team.member, team, word_bytes(), filename="fixed.docx").json()
    before = snapshot(auth_environment, team.project["id"])
    old, new = inspect(auth_environment, team, first), inspect(auth_environment, team, second)
    assert old["status"] == "passed"
    assert new["status"] == "issues"
    assert old["spec_version_id"] == old_spec["id"] == first["spec_version_id"]
    assert new["spec_version_id"] == new_spec["id"] == second["spec_version_id"]
    assert old["spec_version"] == old_spec["version"]
    assert old["sha256"] == first["sha256"] == new["sha256"]
    assert snapshot(auth_environment, team.project["id"]) == before


def test_scoped_formal_lookup_excludes_other_project_shared_material_and_unknown(auth_environment):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team).json()
    shared = upload_material(team.member, team.project["id"]).json()

    class NeverStorage:
        def path(self, digest):
            pytest.fail("An ineligible source must not touch storage")

    for project_id, delivery_id in (
        (uuid4(), UUID(formal["id"])),
        (UUID(team.project["id"]), UUID(shared["id"])),
        (UUID(team.project["id"]), uuid4()),
    ):
        with pytest.raises(FormatCheckError) as failed:
            inspect_delivery_format(
                auth_environment.sessions,
                project_id=project_id,
                delivery_id=delivery_id,
                storage=NeverStorage(),
            )
        assert failed.value.code == "source_unavailable"


def test_private_draft_only_eligible_after_formal_confirmation(auth_environment):
    team = draft_team(auth_environment)
    with pytest.raises(FormatCheckError) as failed:
        inspect(auth_environment, team, team.draft)
    assert failed.value.code == "source_unavailable"
    formal = confirm(team).json()
    result = inspect(auth_environment, team, formal)
    assert result["status"] == "unsupported"
    assert result["delivery_id"] == formal["id"]


@pytest.mark.parametrize("damage", ["missing", "same_size_corrupt", "truncated"])
def test_source_integrity_failure_is_fixed_and_never_changes_review_state(auth_environment, damage):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team, b"original-content").json()
    storage = storage_for(auth_environment)
    path = storage.path(formal["sha256"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"modified-content" if damage == "same_size_corrupt" else b"short")
    before = snapshot(auth_environment, team.project["id"])
    with pytest.raises(FormatCheckError) as failed:
        inspect(auth_environment, team, formal, storage=storage)
    assert failed.value.code == ("source_unavailable" if damage == "missing" else "source_mismatch")
    assert str(path) not in str(failed.value) and "original-content" not in str(failed.value)
    assert snapshot(auth_environment, team.project["id"]) == before


def test_max_size_checked_before_storage_and_no_db_connection_kept_during_read(auth_environment):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team, b"fixed-content").json()
    with auth_environment.sessions() as db:
        pool = db.get_bind().pool

    class InspectStorage:
        touched = False

        def path(self, digest):
            self.touched = True
            assert pool.checkedout() == 0
            return storage_for(auth_environment).path(digest)

    storage = InspectStorage()
    with pytest.raises(FormatCheckError) as failed:
        inspect(
            auth_environment,
            team,
            formal,
            storage=storage,
            limits=replace(ExtractionLimits(), max_file_bytes=1),
        )
    assert failed.value.code == "limit_exceeded" and not storage.touched
    assert inspect(auth_environment, team, formal, storage=storage)["status"] == "unsupported"
    assert storage.touched
