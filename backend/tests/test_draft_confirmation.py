import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_agent_context import context_team
from test_agents import bearer
from test_auth import CSRF_HEADERS
from test_modules import publish_modules
from test_specs import publish_spec, specification

from app.models import AgentDraft, DeliveryVersion, Event
from app.models.immutable import SnapshotImmutableError
from app.services.model_disclosure import DISCLOSURE_VERSION


def draft_team(environment, *, with_spec=True):
    team = context_team(environment, with_spec=with_spec)
    team.agent = environment.make_client()
    team.file_bytes = b"original private draft content\n"
    response = team.agent.post(
        f"/api/agent/tasks/{team.modules[1]['id']}/drafts",
        files={"file": ("本人待确认.md", team.file_bytes, "text/markdown")},
        data={"handoff_note": "结论和来源仍为上传时的说明。"},
        headers=bearer(team.token),
    )
    assert response.status_code == 201, response.text
    team.draft = response.json()["content"]
    team.path = f"/api/projects/{team.project['id']}/drafts"
    team.detail = f"{team.path}/{team.draft['id']}"
    return team


def confirm(team, *, client=None, body=None, headers=None):
    return (client or team.member).post(
        team.detail + "/confirm",
        json={
            "ai_participation": "primary",
            "ai_note": "本人已核查 AI 草稿内容。",
            "disclosure_version": DISCLOSURE_VERSION,
        }
        if body is None
        else body,
        headers=CSRF_HEADERS if headers is None else headers,
    )


def count_deliveries(environment):
    with environment.sessions() as db:
        return len(db.scalars(select(DeliveryVersion)).all())


def test_pending_draft_metadata_and_bytes_are_visible_only_to_the_owner(auth_environment):
    team = draft_team(auth_environment)
    listed = team.member.get(team.path)
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert len(listed.json()) == 1
    own = listed.json()[0]
    assert own["id"] == team.draft["id"]
    assert own["status"] == "pending_confirmation"
    assert own["delivery_id"] is None
    assert own["agent_name"] == "文献助手"
    assert own["owner_id"] == team.member_id
    assert own["handoff_note"] == team.draft["handoff_note"]
    assert own["sha256"] == hashlib.sha256(team.file_bytes).hexdigest()
    assert team.member.get(team.detail).json() == own
    assert team.member.get(own["download_url"]).content == team.file_bytes
    # Give the other member the same module too: joint ownership is not draft ownership.
    revised = deepcopy(team.modules)
    revised[1]["assignee_ids"].append(team.other_member_id)
    publish_modules(team, revised)
    for client in (team.leader, team.other_member):
        assert client.get(team.path).json() == []
        for suffix in ("", "/download"):
            assert client.get(team.detail + suffix).status_code == 404
        assert confirm(team, client=client).status_code == 404
    anonymous = auth_environment.make_client()
    assert anonymous.get(team.path).status_code == 401
    assert confirm(team, client=anonymous).status_code == 401
    assert count_deliveries(auth_environment) == 0


def test_confirmation_requires_explicit_human_declaration_and_keeps_uploaded_bytes(
    auth_environment,
):
    team = draft_team(auth_environment)
    for body in (
        {},
        {"ai_participation": ""},
        {"ai_participation": "unknown"},
        {"ai_participation": "primary", "ai_note": "x" * 2001},
        {"ai_participation": "primary", "ai_note": "bad\x00text"},
        {"ai_participation": "primary", "owner_id": team.leader_id},
        {"ai_participation": "primary", "handoff_note": "不能改写原稿交接说明"},
    ):
        assert confirm(team, body=body).status_code == 422
    assert confirm(team, headers={}).status_code == 403
    assert (
        confirm(team, headers={**CSRF_HEADERS, "Origin": "https://attacker.invalid"}).status_code
        == 403
    )
    assert count_deliveries(auth_environment) == 0
    result = confirm(team)
    assert result.status_code == 201, result.text
    delivery = result.json()
    assert delivery["submitted_by"] == team.member_id
    assert delivery["version"] == 1
    assert delivery["ai_participation"] == "primary"
    assert delivery["ai_note"] == "本人已核查 AI 草稿内容。"
    assert delivery["handoff_note"] == team.draft["handoff_note"]
    assert delivery["sha256"] == team.draft["sha256"]
    for client in (team.member, team.leader, team.other_member):
        assert client.get(delivery["download_url"]).content == team.file_bytes
        assert delivery in client.get(f"/api/projects/{team.project['id']}/deliveries").json()
    own = team.member.get(team.detail).json()
    assert own["status"] == "confirmed" and own["delivery_id"] == delivery["id"]
    assert team.other_member.get(team.detail).status_code == 404
    assert confirm(team).status_code == 409
    assert confirm(team, body={"ai_participation": "none"}).status_code == 409
    assert count_deliveries(auth_environment) == 1
    with auth_environment.sessions() as db:
        events = list(db.scalars(select(Event).where(Event.event_type == "agent_draft_confirmed")))
        assert len(events) == 1
        event = events[0]
        assert event.actor_type == "human" and event.actor_id == team.member_id
        assert event.represented_user_id is None
        assert event.details["ai_participation"] == "primary"
        assert event.details["ai_note"] == delivery["ai_note"]
        assert event.details["delivery_id"] == delivery["id"]


def test_agent_with_owner_cookie_cannot_confirm_or_read_private_human_routes(auth_environment):
    team = draft_team(auth_environment)
    for suffix in ("", "/download"):
        assert team.member.get(team.detail + suffix, headers=bearer(team.token)).status_code == 403
    denied = confirm(team, headers={**CSRF_HEADERS, **bearer(team.token)})
    assert denied.status_code == 403
    assert count_deliveries(auth_environment) == 0
    public = team.leader.get(f"/api/projects/{team.project['id']}/events").json()
    rejections = [e for e in public if e["event_type"] == "agent_permission_denied"]
    assert any(e["details"]["action"] == "confirm_draft" for e in rejections)
    assert all(
        e["actor_type"] == "agent" and e["represented_user_id"] == team.member_id
        for e in rejections
    )
    team.member.post(
        f"/api/projects/{team.project['id']}/agents/{team.agent_id}/revoke", headers=CSRF_HEADERS
    ).raise_for_status()
    # Revocation prevents future agent use, not a human's validation of an existing draft.
    assert confirm(team).status_code == 201


def test_confirmation_checks_latest_assignment_and_binds_confirmation_time_versions(
    auth_environment,
):
    team = draft_team(auth_environment)
    revised = deepcopy(team.modules)
    revised[1]["assignee_ids"] = [team.other_member_id]
    publish_modules(team, revised, "上传后改派")
    assert confirm(team).status_code == 403
    assert count_deliveries(auth_environment) == 0
    assert team.member.get(team.detail).json()["status"] == "pending_confirmation"
    latest_plan = publish_modules(
        team, team.modules, "恢复本人负责并更新方案", argument="确认时方案"
    )
    latest_spec = publish_spec(team.leader, team.project["id"], specification("确认前更新规范"))
    delivery = confirm(team).json()
    assert delivery["plan_version_id"] == latest_plan["id"] != team.plan["id"]
    assert delivery["spec_version_id"] == latest_spec["id"] != team.spec["id"]
    assert delivery["submitted_by"] == team.member_id


def test_confirmation_waits_for_spec_and_rejects_wrong_project_without_losing_private_draft(
    auth_environment,
):
    team = draft_team(auth_environment, with_spec=False)
    assert confirm(team).status_code == 422
    assert count_deliveries(auth_environment) == 0
    assert team.member.get(team.detail).json()["status"] == "pending_confirmation"
    from test_projects import create_project

    foreign = create_project(team.member, "独立项目")
    wrong = f"/api/projects/{foreign['id']}/drafts/{team.draft['id']}"
    assert team.member.get(wrong).status_code == 404
    assert team.member.get(wrong + "/download").status_code == 404
    assert (
        team.member.post(
            wrong + "/confirm", json={"ai_participation": "none"}, headers=CSRF_HEADERS
        ).status_code
        == 404
    )
    publish_spec(team.leader, team.project["id"])
    assert confirm(team).status_code == 201


def test_concurrent_confirmation_creates_one_version_and_frozen_confirmation_record(
    auth_environment,
):
    team = draft_team(auth_environment)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return confirm(team)

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = [
            f.result(timeout=15) for f in [executor.submit(submit), executor.submit(submit)]
        ]
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert count_deliveries(auth_environment) == 1
    with auth_environment.sessions() as db:
        draft = db.get(AgentDraft, UUID(team.draft["id"]))
        draft.filename = "rewrite.md"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        from app.models import DraftConfirmation

        confirmation = db.scalar(select(DraftConfirmation))
        assert str(confirmation.draft_id) == team.draft["id"]
        assert str(confirmation.confirmed_by) == team.member_id
        table = DraftConfirmation.__tablename__
        # The table name comes from static model metadata; no user value enters SQL.
        with pytest.raises(DBAPIError):
            db.connection().execute(text(f'DELETE FROM "{table}"'))
        db.rollback()


def test_failed_confirmation_rolls_back_formal_delivery_event_and_confirmation(
    auth_environment, monkeypatch
):
    team = draft_team(auth_environment)
    from app.services import draft_confirmation

    original_event = draft_confirmation.record_event

    def fail_confirmation_event(db, **values):
        if values["event_type"] == "agent_draft_confirmed":
            # At this point the formal version and confirmation have already been flushed.
            assert db.scalar(select(DeliveryVersion)) is not None
            raise RuntimeError("injected transaction failure")
        return original_event(db, **values)

    with monkeypatch.context() as patch:
        patch.setattr(draft_confirmation, "record_event", fail_confirmation_event)
        with pytest.raises(RuntimeError, match="injected transaction failure"):
            confirm(team)
    assert count_deliveries(auth_environment) == 0
    with auth_environment.sessions() as db:
        from app.models import DraftConfirmation

        assert db.scalar(select(DraftConfirmation)) is None
        assert (
            list(
                db.scalars(
                    select(Event).where(
                        Event.event_type.in_(["delivery_submitted", "agent_draft_confirmed"])
                    )
                )
            )
            == []
        )
    assert team.member.get(team.detail).json()["status"] == "pending_confirmation"
    assert confirm(team).status_code == 201


def test_corrupted_private_file_never_creates_formal_delivery(auth_environment):
    team = draft_team(auth_environment)
    from app.storage import LocalStorage

    storage = LocalStorage(team.member.app.state.settings.storage_dir)
    file = storage.path(team.draft["sha256"])
    file.write_bytes(b"corrupted file")
    assert confirm(team).status_code == 409
    assert count_deliveries(auth_environment) == 0
    assert team.member.get(team.detail).json()["status"] == "pending_confirmation"
    file.write_bytes(team.file_bytes)
    assert confirm(team).status_code == 201
