"""真实正式交付的只读抽取：作用域、文件快照及存储完整性。"""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team

from app.models import DeliveryVersion, DeliveryWorkflow, Event, ReviewJob
from app.review.extract_types import ExtractionError, ExtractionLimits
from app.services.text_extraction import extract_delivery_text
from app.storage import LocalStorage


def storage_for(environment):
    return LocalStorage(environment.client.app.state.settings.storage_dir)


def snapshot(environment, project_id):
    with environment.sessions() as db:
        return {
            model.__tablename__: [
                dict(row)
                for row in db.execute(
                    select(model.__table__).order_by(*model.__table__.primary_key.columns)
                ).mappings()
            ]
            for model in (DeliveryVersion, DeliveryWorkflow, ReviewJob, Event)
        }


def extract(environment, team, delivery, *, storage=None, limits=None):
    return extract_delivery_text(
        environment.sessions,
        project_id=UUID(team.project["id"]),
        delivery_id=UUID(delivery["id"]),
        storage=storage if storage is not None else storage_for(environment),
        limits=limits,
    )


def test_formal_old_and_new_versions_extract_their_own_bytes_without_writes(auth_environment):
    team = ready_team(auth_environment)
    first = upload_delivery(team.member, team, "第一版\n\n证据A".encode()).json()
    second = upload_delivery(team.member, team, "第二版\n证据B".encode()).json()
    before = snapshot(auth_environment, team.project["id"])
    old = extract(auth_environment, team, first)
    new = extract(auth_environment, team, second)
    assert [item.text for item in old.segments] == ["第一版", "证据A"]
    assert [item.location.line_start for item in old.segments] == [1, 3]
    assert [item.text for item in new.segments] == ["第二版", "证据B"]
    assert old.sha256 == first["sha256"] and new.sha256 == second["sha256"]
    assert snapshot(auth_environment, team.project["id"]) == before


def test_confirmed_agent_draft_is_eligible_only_as_its_formal_version(auth_environment):
    team = draft_team(auth_environment)
    with pytest.raises(ExtractionError) as failed:
        extract(auth_environment, team, team.draft)
    assert failed.value.code == "source_unavailable"
    formal = confirm(team).json()
    result = extract(auth_environment, team, formal)
    assert [item.text for item in result.segments] == ["original private draft content"]
    assert result.sha256 == formal["sha256"] == team.draft["sha256"]


def test_other_project_private_draft_shared_material_and_unknown_id_do_not_open_storage(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team).json()
    shared = upload_material(team.member, team.project["id"]).json()

    class UnreachableStorage:
        def path(self, digest):
            pytest.fail("A missing scoped formal delivery must not touch storage")

    for project_id, delivery_id in (
        (uuid4(), UUID(formal["id"])),
        (UUID(team.project["id"]), UUID(shared["id"])),
        (UUID(team.project["id"]), uuid4()),
    ):
        with pytest.raises(ExtractionError) as failed:
            extract_delivery_text(
                auth_environment.sessions,
                project_id=project_id,
                delivery_id=delivery_id,
                storage=UnreachableStorage(),
            )
        assert failed.value.code == "source_unavailable"


@pytest.mark.parametrize("change", ["missing", "same_length_corrupt", "truncated"])
def test_missing_or_corrupt_stored_file_is_unfinished_without_mutating_delivery(
    auth_environment, change
):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team, b"original-content").json()
    storage = storage_for(auth_environment)
    path = storage.path(formal["sha256"])
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(b"modified-content" if change == "same_length_corrupt" else b"short")
    before = snapshot(auth_environment, team.project["id"])
    with pytest.raises(ExtractionError) as failed:
        extract(auth_environment, team, formal)
    assert failed.value.code == ("source_unavailable" if change == "missing" else "source_mismatch")
    assert str(path) not in str(failed.value) and "original-content" not in str(failed.value)
    assert snapshot(auth_environment, team.project["id"]) == before


def test_known_oversize_rejected_before_file_access_and_no_transaction_kept_during_read(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload_delivery(team.member, team, b"a\nb\n").json()

    class InspectStorage:
        def path(self, digest) -> Path:
            assert pool.checkedout() == 0, "Document parsing must not retain a DB connection"
            return storage_for(auth_environment).path(digest)

    with auth_environment.sessions() as db:
        pool = db.get_bind().pool
    with pytest.raises(ExtractionError) as failed:
        extract(
            auth_environment,
            team,
            formal,
            storage=InspectStorage(),
            limits=replace(ExtractionLimits(), max_file_bytes=2),
        )
    assert failed.value.code == "limit_exceeded"
    result = extract(auth_environment, team, formal, storage=InspectStorage())
    assert [item.text for item in result.segments] == ["a", "b"]
