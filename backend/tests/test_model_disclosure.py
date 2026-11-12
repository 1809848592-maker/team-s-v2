"""M3-13：明示同意、首次提交告知和任何外发前的拒绝门禁。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_agents import bearer, register_agent
from test_auth import CSRF_HEADERS, login, register
from test_deliveries import delivery_path, ready_team, upload_delivery
from test_draft_confirmation import confirm, draft_team
from test_llm_integration import complete, model_client
from test_llm_usage import formal_job
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, wait_for_records
from test_modules import publish_modules, team_modules
from test_projects import accept, invite
from test_review_jobs import claim, database_time
from test_specs import publish_spec

from app.models import (
    DailyBudget,
    DeliveryVersion,
    Event,
    ModelCall,
    Project,
    ProjectMember,
    ReviewAdmission,
    ReviewJob,
)
from app.models.immutable import SnapshotImmutableError
from app.models.model_disclosure import ProjectModelConsent, SubmissionDisclosure
from app.review.runner import DeliveryReviewInput
from app.services import model_disclosure as disclosure
from app.services import review_pipeline
from app.services.auth import COOKIE_NAME
from app.services.projects import LeaderRequiredError


def raw_project(client, **changes):
    return client.post(
        "/api/projects", json={"name": "无默认外发项目", **changes}, headers=CSRF_HEADERS
    )


def unconsented_team(environment):
    leader, member, other = environment.client, environment.make_client(), environment.make_client()
    team = SimpleNamespace(
        leader=leader,
        leader_id=register(leader, "leader").json()["id"],
        member=member,
        member_id=register(member, "member").json()["id"],
        other_member=other,
        other_member_id=register(other, "other-member").json()["id"],
        project=raw_project(leader).json(),
    )
    for client in (member, other):
        assert accept(client, invite(leader, team.project["id"])).status_code == 200
    team.modules = team_modules(team)
    team.plan = publish_modules(team, team.modules)
    team.spec = publish_spec(leader, team.project["id"])
    return team


def path(team):
    return f"/api/projects/{team.project['id']}/model-consent"


def consent(team, client=None, version=None, headers=None):
    return (client or team.leader).post(
        path(team),
        json={"disclosure_version": version or disclosure.DISCLOSURE_VERSION},
        headers=CSRF_HEADERS if headers is None else headers,
    )


def upload_notice(team, *, version=None, client=None, module_id=None):
    data = {"ai_participation": "none"}
    if version is not None:
        data["disclosure_version"] = version
    return (client or team.member).post(
        delivery_path(team, module_id),
        files={"file": ("正式交付.md", b"private proof")},
        data=data,
        headers=CSRF_HEADERS,
    )


def count(environment, model):
    with environment.sessions() as db:
        return db.scalar(select(func.count()).select_from(model))


def test_notice_requires_login_and_does_not_promise_unverified_api_training_or_retention(
    auth_environment,
):
    assert auth_environment.client.get("/api/model-disclosure").status_code == 401
    register(auth_environment.client)
    response = auth_environment.client.get("/api/model-disclosure")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    notice = response.json()
    assert notice["version"] == disclosure.DISCLOSURE_VERSION
    assert notice["provider"] == "DeepSeek" and notice["consent_required"] is True
    assert "尚未核实" in notice["uncertainty"] and "训练" in notice["uncertainty"]
    assert "保留期限" in notice["uncertainty"] and "退出配置" in notice["uncertainty"]
    assert "Crossref" in notice["summary"] and "OpenAlex" in notice["summary"]
    assert notice["terms_url"].startswith("https://cdn.deepseek.com/")
    assert notice["privacy_url"].startswith("https://cdn.deepseek.com/")


def test_default_project_creation_leaves_ai_unauthorized_and_can_be_consented_later(
    auth_environment,
):
    team = unconsented_team(auth_environment)
    response = team.member.get(f"/api/projects/{team.project['id']}/model-disclosure")
    assert response.json()["project_consent"] is None
    assert not response.json()["can_consent"] and not response.json()["submission_acknowledged"]
    with auth_environment.sessions() as db:
        with pytest.raises(disclosure.ModelConsentRequiredError):
            disclosure.require_review_consent(db, UUID(team.project["id"]))
    response = consent(team)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["project_consent"]["actor_id"] == team.leader_id
    assert response.json()["can_consent"] and not response.json()["submission_acknowledged"]
    assert consent(team).json()["project_consent"] == response.json()["project_consent"]
    assert count(auth_environment, ProjectModelConsent) == 1
    with auth_environment.sessions() as db:
        item = db.scalar(select(ProjectModelConsent))
        assert item.notice == disclosure.notice()
        assert db.get(Event, item.event_id).event_type == "model_data_transfer_consented"
        disclosure.require_review_consent(db, UUID(team.project["id"]))


@pytest.mark.parametrize("version", [None, "old-notice"])
def test_create_with_consent_requires_current_notice_and_rolls_back_entire_project(
    auth_environment, version
):
    register(auth_environment.client)
    before_events = count(auth_environment, Event)
    payload = {"data_transfer_consent": True}
    if version is not None:
        payload["disclosure_version"] = version
    response = raw_project(auth_environment.client, **payload)
    assert response.status_code == 409
    for model in (Project, ProjectMember, ProjectModelConsent):
        assert count(auth_environment, model) == 0
    assert count(auth_environment, Event) == before_events


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"disclosure_version": ""},
        {"disclosure_version": "v" * 81},
        {"disclosure_version": "current", "actor_id": str(uuid4())},
    ],
)
def test_consent_request_strict_shape_never_records_ack(auth_environment, payload):
    team = unconsented_team(auth_environment)
    assert team.leader.post(path(team), json=payload, headers=CSRF_HEADERS).status_code == 422
    assert count(auth_environment, ProjectModelConsent) == 0


@pytest.mark.parametrize("value", ["true", 1, None])
def test_consent_boolean_cannot_be_coerced(auth_environment, value):
    register(auth_environment.client)
    assert raw_project(auth_environment.client, data_transfer_consent=value).status_code == 422
    assert count(auth_environment, Project) == 0


def test_creation_records_explicit_leader_consent_but_never_submission_ack(auth_environment):
    identity = register(auth_environment.client).json()
    response = raw_project(
        auth_environment.client,
        data_transfer_consent=True,
        disclosure_version=disclosure.DISCLOSURE_VERSION,
    )
    assert response.status_code == 201
    with auth_environment.sessions() as db:
        consent_record = db.scalar(select(ProjectModelConsent))
        assert consent_record.project_id == UUID(response.json()["id"])
        assert consent_record.actor_id == UUID(identity["id"])
        assert consent_record.notice == disclosure.notice()
    assert count(auth_environment, SubmissionDisclosure) == 0


def test_two_leaders_consent_concurrently_only_one_record_and_event(auth_environment):
    team = unconsented_team(auth_environment)
    with auth_environment.sessions() as db:
        db.get(
            ProjectMember, (UUID(team.project["id"]), UUID(team.other_member_id))
        ).role = "leader"
        db.commit()
    barrier = Barrier(2)

    def accept_as(identity):
        barrier.wait(timeout=5)
        with auth_environment.sessions() as db:
            return disclosure.accept_project_consent(
                db, UUID(team.project["id"]), UUID(identity), disclosure.DISCLOSURE_VERSION
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(accept_as, (team.leader_id, team.other_member_id)))
    assert result[0]["project_consent"] == result[1]["project_consent"]
    assert count(auth_environment, ProjectModelConsent) == 1
    with auth_environment.sessions() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(Event).where(Event.event_type == "model_data_transfer_consented")
                    )
                )
            )
            == 1
        )


def test_nonleader_admin_csrf_outsider_and_anonymous_cannot_accept(auth_environment):
    team = unconsented_team(auth_environment)
    admin = auth_environment.make_client(platform_admin_user_ids=team.member_id)
    assert login(admin, "member").json()["is_admin"]
    assert consent(team, client=team.member).status_code == 403
    assert consent(team, client=admin).status_code == 403
    assert consent(team, headers={}).status_code == 403
    outsider = auth_environment.make_client()
    register(outsider, "outsider")
    assert consent(team, client=outsider).status_code == 404
    assert outsider.get(f"/api/projects/{team.project['id']}/model-disclosure").status_code == 404
    assert consent(team, client=auth_environment.make_client()).status_code == 401
    assert count(auth_environment, ProjectModelConsent) == 0


@pytest.mark.parametrize("owner", ["leader", "member"])
@pytest.mark.parametrize("cookie", [False, True])
def test_agent_cannot_accept_even_with_owner_cookie_and_rejection_is_audited(
    auth_environment, owner, cookie
):
    team = unconsented_team(auth_environment)
    client = getattr(team, owner)
    token = register_agent(client, team.project["id"])["token"]
    response = consent(
        team,
        client=client if cookie else auth_environment.make_client(),
        headers={**CSRF_HEADERS, **bearer(token)},
    )
    assert response.status_code == 403
    assert count(auth_environment, ProjectModelConsent) == 0
    with auth_environment.sessions() as db:
        rejected = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert rejected is not None and rejected.actor_type == "agent"
        assert rejected.details["action"] == "consent_model_data_transfer"


@pytest.mark.parametrize("cookie", [False, True])
def test_mcp_has_no_consent_tool_even_for_leader_with_cookie(auth_environment, mcp_network, cookie):
    team = unconsented_team(auth_environment)
    created = register_agent(team.leader, team.project["id"], "负责人智能体")
    headers = {"Cookie": f"{COOKIE_NAME}={team.leader.cookies.get(COOKIE_NAME)}"} if cookie else {}

    async def scenario():
        async with official_client(mcp_network, created["token"], headers=headers) as client:
            assert "consent_model_data_transfer" not in {
                item.name for item in (await client.list_tools()).tools
            }
            result = await client.call_tool(
                "consent_model_data_transfer",
                {
                    "project_id": team.project["id"],
                    "disclosure_version": disclosure.DISCLOSURE_VERSION,
                },
            )
            assert result.is_error

    asyncio.run(scenario())
    records = wait_for_records(auth_environment, team, "agent_tool_call", 1)
    assert records[0].details == {"tool": "unknown", "outcome": "error"}
    assert count(auth_environment, ProjectModelConsent) == 0
    with auth_environment.sessions() as db:
        rejection = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert rejection.actor_id == created["agent"]["id"]
        assert rejection.details["action"] == "mcp_unknown_tool"


@pytest.mark.parametrize("version", [None, "old-notice"])
def test_first_submission_missing_or_stale_notice_fails_before_storage_or_delivery(
    auth_environment, version
):
    team = ready_team(auth_environment)
    assert upload_notice(team, version=version).status_code == 409
    for model in (DeliveryVersion, ReviewJob, SubmissionDisclosure):
        assert count(auth_environment, model) == 0
    assert not list(team.member.app.state.settings.storage_dir.rglob("*"))


def test_first_ack_is_once_per_user_and_never_enables_model_without_leader_consent(
    auth_environment,
):
    team = unconsented_team(auth_environment)
    first = upload_notice(team, version=disclosure.DISCLOSURE_VERSION)
    assert first.status_code == 201, first.text
    assert upload_notice(team).status_code == 201
    assert count(auth_environment, SubmissionDisclosure) == 1
    assert count(auth_environment, ProjectModelConsent) == 0
    assert upload_notice(team, version="old-notice").status_code == 409
    second_user = upload_notice(
        team,
        version=disclosure.DISCLOSURE_VERSION,
        client=team.other_member,
        module_id=team.modules[1]["id"],
    )
    assert second_user.status_code == 201
    assert count(auth_environment, SubmissionDisclosure) == 2
    with auth_environment.sessions() as db:
        ack = db.scalar(
            select(SubmissionDisclosure).where(SubmissionDisclosure.user_id == UUID(team.member_id))
        )
        assert ack.delivery_id == UUID(first.json()["id"]) and ack.notice == disclosure.notice()
        assert db.get(Event, ack.event_id).event_type == "submission_data_transfer_disclosed"
        with pytest.raises(disclosure.ModelConsentRequiredError):
            disclosure.require_review_consent(db, UUID(team.project["id"]))


def test_draft_confirmation_requires_notice_and_preserves_original_bytes(auth_environment):
    team = draft_team(auth_environment)
    assert confirm(team, body={"ai_participation": "primary"}).status_code == 409
    assert count(auth_environment, SubmissionDisclosure) == 0
    response = confirm(team)
    assert response.status_code == 201, response.text
    item = response.json()
    assert team.member.get(item["download_url"]).content == team.file_bytes
    with auth_environment.sessions() as db:
        assert db.scalar(select(SubmissionDisclosure.delivery_id)) == UUID(item["id"])


def test_ack_event_failure_rolls_back_formal_delivery_job_and_disclosure(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    actual = disclosure.record_event

    def fail_after_event(*args, **values):
        actual(*args, **values)
        if values["event_type"] == "submission_data_transfer_disclosed":
            raise RuntimeError("fixed late disclosure failure")

    monkeypatch.setattr(disclosure, "record_event", fail_after_event)
    with pytest.raises(RuntimeError, match="fixed late"):
        upload_notice(team, version=disclosure.DISCLOSURE_VERSION)
    for model in (DeliveryVersion, ReviewJob, SubmissionDisclosure):
        assert count(auth_environment, model) == 0
    with auth_environment.sessions() as db:
        assert not list(
            db.scalars(
                select(Event).where(Event.event_type == "submission_data_transfer_disclosed")
            )
        )


def test_no_project_consent_stops_transport_before_call_or_any_budget_reservation(auth_environment):
    team = unconsented_team(auth_environment)
    job = formal_job(auth_environment, team)
    attempted = []

    def forbidden(request):
        attempted.append(request)
        pytest.fail("No consent must prevent even MockTransport entry")

    client = model_client(auth_environment, transport=httpx2.MockTransport(forbidden))
    with pytest.raises(disclosure.ModelConsentRequiredError):
        complete(client, job, uuid4())
    assert attempted == []
    for model in (ModelCall, DailyBudget, ReviewAdmission):
        assert count(auth_environment, model) == 0


def test_no_consent_blocks_entire_pipeline_before_model_or_citation_database(
    auth_environment, monkeypatch
):
    team = unconsented_team(auth_environment)
    upload_delivery(team.member, team)
    claimed = claim(auth_environment, database_time(auth_environment))
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    operations = []

    def forbidden(*args, **kwargs):
        operations.append(True)
        pytest.fail("Unconsented pipeline reached external/model stage")

    for name in (
        "inspect_delivery_format",
        "review_delivery_spec",
        "review_delivery_citations",
        "review_delivery_reasoning",
    ):
        monkeypatch.setattr(review_pipeline, name, forbidden)
    with pytest.raises(disclosure.ModelConsentRequiredError):
        review_pipeline.execute_pipeline(
            auth_environment.sessions, review, storage=None, client=None, database_client=None
        )
    assert operations == []
    assert count(auth_environment, ModelCall) == 0


@pytest.mark.parametrize("model", [ProjectModelConsent, SubmissionDisclosure])
def test_consent_and_ack_snapshots_reject_orm_and_raw_sql_mutation(auth_environment, model):
    team = ready_team(auth_environment)
    upload_delivery(team.member, team)
    with auth_environment.sessions() as db:
        item = db.scalar(select(model))
        original_id = item.id
        item.disclosure_version = "rewrite"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        for operation in (delete(model), update(model).values(disclosure_version="rewrite")):
            with pytest.raises(SnapshotImmutableError):
                db.execute(operation)
            db.rollback()
        schema = db.get_bind().get_execution_options()["schema_translate_map"][None]
        table = f'"{schema}"."{model.__tablename__}"'
        for statement in (
            f"UPDATE {table} SET disclosure_version='rewrite'",
            f"DELETE FROM {table}",
            f"TRUNCATE {table}",
        ):
            with pytest.raises(DBAPIError) as error:
                db.execute(text(statement))
            assert error.value.orig.sqlstate == "55000"
            db.rollback()
        assert db.get(model, original_id).disclosure_version == disclosure.DISCLOSURE_VERSION


def test_new_disclosure_version_invalidates_old_consent_and_first_ack(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    upload_delivery(team.member, team)
    old = disclosure.DISCLOSURE_VERSION
    monkeypatch.setattr(disclosure, "DISCLOSURE_VERSION", "deepseek-new-notice")
    response = team.leader.get(f"/api/projects/{team.project['id']}/model-disclosure").json()
    assert response["project_consent"] is None and not response["submission_acknowledged"]
    assert consent(team, version=old).status_code == 409
    assert upload_notice(team).status_code == 409
    assert consent(team).status_code == 200
    assert upload_notice(team, version=disclosure.DISCLOSURE_VERSION).status_code == 201
    assert count(auth_environment, ProjectModelConsent) == 2
    assert count(auth_environment, SubmissionDisclosure) == 2


def test_cached_former_leader_cannot_consent_after_role_change(auth_environment):
    team = unconsented_team(auth_environment)
    key = (UUID(team.project["id"]), UUID(team.leader_id))
    with auth_environment.sessions() as cached:
        member = cached.get(ProjectMember, key)
        assert member.role == "leader"
        with auth_environment.sessions() as db:
            db.get(ProjectMember, key).role = "member"
            db.commit()
        with pytest.raises(LeaderRequiredError):
            disclosure.accept_project_consent(cached, *key, disclosure.DISCLOSURE_VERSION)
    assert count(auth_environment, ProjectModelConsent) == 0
