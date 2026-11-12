"""公开重试只排队；真实 PostgreSQL 验证权限、并发、原子性及人类通道。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event as ThreadEvent
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_context import context_team
from test_agents import bearer
from test_auth import CSRF_HEADERS, login, register
from test_deliveries import upload_delivery
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, recorded, wait_for_records
from test_modules import module_publication, publish_modules
from test_projects import create_project
from test_review_jobs import claim, database_time, fail, job_for

from app.models import (
    DeliveryWorkflow,
    Event,
    ModelCall,
    Project,
    ProjectMember,
    ReviewAdmission,
    ReviewJob,
)
from app.services import plans, projects, review_jobs, review_retry
from app.services.auth import COOKIE_NAME


def failed_team(environment, *, shared=False):
    team = context_team(environment)
    team.module_index = 2 if shared else 1
    response = upload_delivery(team.member, team, module_id=team.modules[team.module_index]["id"])
    assert response.status_code == 201, response.text
    team.delivery = response.json()
    claimed = claim(environment, database_time(environment), max_attempts=1)
    assert claimed is not None
    assert fail(environment, claimed, database_time(environment), retryable=False)
    team.review_path = f"/api/projects/{team.project['id']}/deliveries/{team.delivery['id']}/review"
    team.retry_path = f"{team.review_path}/retry"
    team.revision = team.member.get(team.review_path).json()["workflow_revision"]
    return team


def body(team):
    return {"expected_revision": team.revision}


def test_submitter_retry_preserves_attempt_and_job_records_human_and_no_model(auth_environment):
    team = failed_team(auth_environment)
    with auth_environment.sessions() as db:
        before = job_for(db, team.delivery)
        identity, attempts, maximum = before.id, before.attempt, before.max_attempts
    response = team.member.post(team.retry_path, json=body(team), headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "retry_wait"
    assert response.json()["attempt"] == attempts
    assert response.json()["max_attempts"] == maximum + 3
    assert not team.member.get(team.review_path).json()["can_retry"]
    with auth_environment.sessions() as db:
        job = job_for(db, team.delivery)
        assert job.id == identity and job.finished_at is None
        assert (
            db.get(DeliveryWorkflow, UUID(team.delivery["id"])).state == "ai_review_partial_failure"
        )
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewAdmission)) == 0
        event = db.scalar(select(Event).where(Event.event_type == "review_retry_requested"))
        assert event.actor_type == "human" and event.actor_id == team.member_id
        assert event.details["attempt"] == attempts
        assert event.details["job_id"] == str(identity)
    assert (
        team.member.post(team.retry_path, json=body(team), headers=CSRF_HEADERS).status_code == 409
    )


def test_current_coassignee_can_retry_but_reassignment_removes_permission(auth_environment):
    team = failed_team(auth_environment, shared=True)
    assert team.other_member.get(team.review_path).json()["can_retry"]
    changed = deepcopy(team.modules)
    changed[2]["assignee_ids"] = [team.member_id]
    publish_modules(team, changed)
    assert not team.other_member.get(team.review_path).json()["can_retry"]
    denied = team.other_member.post(team.retry_path, json=body(team), headers=CSRF_HEADERS)
    assert denied.status_code == 403
    changed[2]["assignee_ids"].append(team.other_member_id)
    publish_modules(team, changed)
    allowed = team.other_member.post(team.retry_path, json=body(team), headers=CSRF_HEADERS)
    assert allowed.status_code == 200, allowed.text


@pytest.mark.parametrize("person", ["leader", "other_member"])
def test_project_role_alone_does_not_allow_retry(auth_environment, person):
    team = failed_team(auth_environment)
    client = getattr(team, person)
    assert not client.get(team.review_path).json()["can_retry"]
    assert client.post(team.retry_path, json=body(team), headers=CSRF_HEADERS).status_code == 403


def test_outsider_or_cross_project_or_missing_delivery_is_hidden(auth_environment):
    team = failed_team(auth_environment)
    outsider = auth_environment.make_client()
    assert register(outsider, "outside-review-retry").status_code == 201
    assert outsider.post(team.retry_path, json=body(team), headers=CSRF_HEADERS).status_code == 404
    other = create_project(team.member, "other-project")
    path = team.retry_path.replace(team.project["id"], other["id"])
    assert team.member.post(path, json=body(team), headers=CSRF_HEADERS).status_code == 404
    path = team.retry_path.replace(team.delivery["id"], str(uuid4()))
    assert team.member.post(path, json=body(team), headers=CSRF_HEADERS).status_code == 404


@pytest.mark.parametrize("headers", [{}, {**CSRF_HEADERS, "Origin": "https://other.example"}])
def test_retry_requires_csrf_and_same_origin(auth_environment, headers):
    team = failed_team(auth_environment)
    assert team.member.post(team.retry_path, json=body(team), headers=headers).status_code == 403
    anonymous = auth_environment.make_client()
    assert anonymous.post(team.retry_path, json=body(team), headers=CSRF_HEADERS).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_revision": True},
        {"expected_revision": "2"},
        {"expected_revision": 0},
        {"expected_revision": 2.5},
        {"expected_revision": 2147483648},
        {"expected_revision": 2, "actor_id": "forged"},
    ],
)
def test_retry_schema_never_coerces_or_allows_forged_fields(auth_environment, payload):
    team = failed_team(auth_environment)
    response = team.member.post(team.retry_path, json=payload, headers=CSRF_HEADERS)
    assert response.status_code == 422
    with auth_environment.sessions() as db:
        assert job_for(db, team.delivery).status == "failed"
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_stale_revision_conflicts_without_requeue(auth_environment):
    team = failed_team(auth_environment)
    response = team.member.post(
        team.retry_path, json={"expected_revision": team.revision + 1}, headers=CSRF_HEADERS
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    with auth_environment.sessions() as db:
        assert job_for(db, team.delivery).status == "failed"


@pytest.mark.parametrize("status", ["queued", "running", "retry_wait", "paused", "succeeded"])
def test_only_exhausted_failed_job_can_be_manually_retried(auth_environment, status):
    team = failed_team(auth_environment)
    with auth_environment.sessions() as db:
        job = job_for(db, team.delivery)
        job.status = status
        if status == "running":
            job.claim_token = uuid4()
            job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
    assert not team.member.get(team.review_path).json()["can_retry"]
    assert (
        team.member.post(team.retry_path, json=body(team), headers=CSRF_HEADERS).status_code == 409
    )


def test_late_human_event_failure_rolls_back_requeue_and_worker_event(
    auth_environment, monkeypatch
):
    team = failed_team(auth_environment)
    with auth_environment.sessions() as db:
        count = db.scalar(select(func.count()).select_from(Event))
        maximum = job_for(db, team.delivery).max_attempts

    def fail_event(*args, **kwargs):
        raise RuntimeError("fixed event failure")

    monkeypatch.setattr(review_retry, "record_event", fail_event)
    with (
        auth_environment.sessions() as db,
        pytest.raises(RuntimeError, match="fixed event failure"),
    ):
        review_retry.request_retry(
            db,
            UUID(team.project["id"]),
            UUID(team.delivery["id"]),
            UUID(team.member_id),
            expected_revision=team.revision,
        )
    with auth_environment.sessions() as db:
        job = job_for(db, team.delivery)
        assert job.status == "failed" and job.max_attempts == maximum
        assert db.scalar(select(func.count()).select_from(Event)) == count


def test_concurrent_retries_requeue_same_job_only_once(auth_environment):
    team = failed_team(auth_environment)

    def request():
        with auth_environment.sessions() as db:
            try:
                review_retry.request_retry(
                    db,
                    UUID(team.project["id"]),
                    UUID(team.delivery["id"]),
                    UUID(team.member_id),
                    expected_revision=team.revision,
                )
                return "queued"
            except review_jobs.ReviewJobConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: request(), range(2))) == ["conflict", "queued"]
    with auth_environment.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ReviewJob)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_type == "review_retry_requested")
            )
            == 1
        )


@pytest.mark.parametrize("cookie", [False, True])
def test_agent_bearer_even_with_owner_cookie_is_denied_and_audited(auth_environment, cookie):
    team = failed_team(auth_environment)
    client = team.member if cookie else auth_environment.make_client()
    response = client.post(
        team.retry_path, json=body(team), headers={**CSRF_HEADERS, **bearer(team.token)}
    )
    assert response.status_code == 403
    with auth_environment.sessions() as db:
        denial = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert denial.details["action"] == "retry_review"
        assert denial.represented_user_id == UUID(team.member_id)
        assert job_for(db, team.delivery).status == "failed"


@pytest.mark.parametrize("change", ["remove_membership", "reassign_module"])
def test_project_lock_waiter_rechecks_membership_and_assignment_before_requeue(
    auth_environment, change
):
    team = failed_team(auth_environment, shared=True)
    project_id, delivery_id = UUID(team.project["id"]), UUID(team.delivery["id"])
    # A submitter must still be a member; a coassignee must still own the current module.
    user_id = UUID(team.member_id if change == "remove_membership" else team.other_member_id)
    started, waiter_pids = ThreadEvent(), []

    def waiting_request():
        with auth_environment.sessions() as db:
            cached_member = db.get(ProjectMember, (project_id, user_id))
            cached_plan = plans.get_latest_plan(db, project_id)
            assert cached_member is not None and cached_plan.id == UUID(team.plan["id"])
            waiter_pids.append(db.scalar(select(func.pg_backend_pid())))
            started.set()
            try:
                review_retry.request_retry(
                    db, project_id, delivery_id, user_id, expected_revision=team.revision
                )
            except projects.ProjectNotFoundError:
                return "not_member"
            except review_retry.ReviewRetryPermissionError:
                return "not_assignee"
            return "unexpected_success"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with auth_environment.sessions() as locking:
            locking.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
            locking_pid = locking.scalar(select(func.pg_backend_pid()))
            waiting = executor.submit(waiting_request)
            try:
                assert started.wait(timeout=5)
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    if locking_pid in locking.scalar(select(func.pg_blocking_pids(waiter_pids[0]))):
                        break
                    assert not waiting.done(), "重试请求在项目锁释放前完成了"
                    ThreadEvent().wait(0.01)
                else:
                    raise AssertionError("重试请求没有等待已有项目锁")
                if change == "remove_membership":
                    locking.delete(locking.get(ProjectMember, (project_id, user_id)))
                    locking.commit()
                else:
                    changed = deepcopy(team.modules)
                    changed[team.module_index]["assignee_ids"] = [team.member_id]
                    plans.publish_plan(
                        locking,
                        project_id,
                        UUID(team.leader_id),
                        module_publication(changed, "持锁期间移交共同负责人的模块"),
                    )
            finally:
                locking.rollback()
        assert waiting.result(timeout=10) == (
            "not_member" if change == "remove_membership" else "not_assignee"
        )
    with auth_environment.sessions() as db:
        job = job_for(db, team.delivery)
        assert job.status == "failed" and job.max_attempts == 1
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewAdmission)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(Event)
                .where(Event.event_type == "review_retry_requested")
            )
            == 0
        )


@pytest.mark.parametrize("project_member", [False, True])
def test_platform_admin_status_never_grants_review_retry_permission(
    auth_environment, project_member
):
    team = failed_team(auth_environment)
    if project_member:
        identity, username, status = team.other_member_id, "other-member", 403
    else:
        outsider = auth_environment.make_client()
        username = "admin-without-project"
        identity = register(outsider, username).json()["id"]
        status = 404
    admin = auth_environment.make_client(platform_admin_user_ids=identity)
    assert login(admin, username).json()["is_admin"] is True
    response = admin.post(team.retry_path, json=body(team), headers=CSRF_HEADERS)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    with auth_environment.sessions() as db:
        assert job_for(db, team.delivery).status == "failed"
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("owner_cookie", [False, True])
def test_mcp_has_no_retry_tool_and_denials_cannot_requeue_or_call_models(
    auth_environment, mcp_network, monkeypatch, owner_cookie
):
    team = failed_team(auth_environment)

    def must_not_run(*args, **kwargs):
        pytest.fail("An agent crossed the human review retry boundary")

    monkeypatch.setattr(review_retry, "request_retry", must_not_run)
    headers = (
        {"Cookie": f"{COOKIE_NAME}={team.member.cookies.get(COOKIE_NAME)}"} if owner_cookie else {}
    )
    private_marker = "PRIVATE_RETRY_CONTENT_MUST_NOT_ENTER_AUDIT"

    async def scenario():
        async with official_client(mcp_network, team.token, headers=headers) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert not names & {"retry_review", "request_retry"}
            for name in ("retry_review", "request_retry"):
                result = await client.call_tool(
                    name,
                    {
                        "project_id": team.project["id"],
                        "delivery_id": team.delivery["id"],
                        **body(team),
                        "content": private_marker,
                    },
                )
                assert result.is_error
                serialized = json.dumps(result.model_dump(mode="json", by_alias=True))
                assert "permission_denied" in serialized and private_marker not in serialized

    asyncio.run(scenario())
    calls = wait_for_records(auth_environment, team, "agent_tool_call", 2)
    assert len(calls) == 2
    assert all(row.details == {"tool": "unknown", "outcome": "error"} for row in calls)
    denials = recorded(auth_environment, team, "agent_permission_denied")
    assert len(denials) == 2
    assert all(row.details["action"] == "mcp_unknown_tool" for row in denials)
    assert all(row.actor_id == str(team.agent_id) for row in denials)
    assert all(row.represented_user_id == UUID(team.member_id) for row in denials)
    assert private_marker not in json.dumps([row.details for row in [*calls, *denials]])
    with auth_environment.sessions() as db:
        assert job_for(db, team.delivery).status == "failed"
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewAdmission)) == 0
