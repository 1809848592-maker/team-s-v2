"""M3-10: real identity channels protect human annotations and agent report reads."""

import asyncio
import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_agent_context import context_team, get_context
from test_agents import bearer
from test_auth import CSRF_HEADERS
from test_deliveries import upload_delivery
from test_mcp import mcp_network as mcp_network
from test_mcp import official_client, recorded, wait_for_records

from app.models import DeliveryVersion, Event, ReviewJob
from app.models.review_report import ReviewFinding, ReviewReport, ReviewSummary
from app.services import projects, review_reports
from app.services.auth import COOKIE_NAME
from app.services.delivery_states import transition_review


@pytest.fixture
def review_team(auth_environment):
    team = context_team(auth_environment)
    response = upload_delivery(team.member, team, module_id=team.modules[1]["id"])
    assert response.status_code == 201, response.text
    team.delivery = response.json()
    team.finding_id = str(uuid4())
    team.review_path = f"/api/projects/{team.project['id']}/deliveries/{team.delivery['id']}/review"
    team.annotation_path = f"{team.review_path}/findings/{team.finding_id}/annotation"
    return team


def annotation(handling="adopted", reason="", revision=0):
    return {"handling": handling, "reason": reason, "expected_revision": revision}


def report(team):
    return {
        "coverage": "completed",
        "opinions": [
            {
                "finding_id": team.finding_id,
                "opinion": {
                    "category": "logic",
                    "severity": "important",
                    "location": {"description": "第1段", "paragraph": 1},
                    "problem": "结论缺少推理步骤",
                    "basis": {"excerpt": "因此本方法适用所有情况。"},
                    "suggestion": "补充推理和适用边界。",
                },
                "handling": "not_adopted",
                "reason": "适用边界已在前一段说明。",
                "actor_id": team.member_id,
                "annotation_revision": 1,
                "duplicate_of": None,
            }
        ],
        "note": "AI 生成，供参考。",
        "state": "returned",
        "report_id": str(uuid4()),
        "round": 1,
        "nonconverged": False,
        "components": {"logic": {"status": "completed"}},
        "history": [],
    }


def persist_report(environment, team, delivery_id):
    """Human route integration starts from persisted review output, with no model calls."""
    with environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(delivery_id))
        job = db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == item.id))
        report_row = ReviewReport(
            delivery_id=item.id,
            job_id=job.id,
            attempt=1,
            workflow_revision=1,
            components={"scope": "fixed fixture"},
        )
        db.add(report_row)
        db.flush()
        summary = ReviewSummary(
            delivery_id=item.id, report_id=report_row.id, status="completed", round=1
        )
        db.add(summary)
        db.flush()
        finding = ReviewFinding(
            summary_id=summary.id, opinion=report(team)["opinions"][0]["opinion"]
        )
        db.add(finding)
        transition_review(
            db,
            item.project_id,
            item.id,
            "returned",
            expected_state="ai_reviewing",
            expected_revision=1,
            actor_id="platform-review-worker",
            actor_type="agent",
        )
        db.commit()
        return str(finding.id)


def test_real_http_annotation_is_recorded_readable_and_revision_checked(
    auth_environment, review_team
):
    team = review_team
    finding_id = persist_report(auth_environment, team, team.delivery["id"])
    path = f"{team.review_path}/findings/{finding_id}/annotation"
    response = team.member.post(
        path, json=annotation("not_adopted", "已补充来源，此处保留原结论。"), headers=CSRF_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["actor_id"] == team.member_id
    assert response.json()["annotation_revision"] == 1
    assert response.json()["handling"] == "not_adopted"
    visible = team.other_member.get(team.review_path)
    assert visible.status_code == 200
    assert visible.json()["opinions"][0]["reason"] == "已补充来源，此处保留原结论。"
    assert team.member.post(path, json=annotation(), headers=CSRF_HEADERS).status_code == 409
    assert (
        team.other_member.post(path, json=annotation(revision=1), headers=CSRF_HEADERS).status_code
        == 403
    )
    assert (
        team.leader.post(path, json=annotation(revision=1), headers=CSRF_HEADERS).status_code == 403
    )
    updated = team.member.post(path, json=annotation(revision=1), headers=CSRF_HEADERS)
    assert updated.status_code == 200
    assert updated.json()["annotation_revision"] == 2
    events = recorded(auth_environment, team, "review_finding_annotated")
    assert len(events) == 2
    assert [row.actor_id for row in events] == [team.member_id, team.member_id]
    assert "已补充来源" not in json.dumps([row.details for row in events], ensure_ascii=False)


def test_real_http_coassignee_can_annotate_and_agent_reads_updated_reason(
    auth_environment, review_team
):
    team = review_team
    response = upload_delivery(team.member, team, module_id=team.modules[2]["id"])
    assert response.status_code == 201
    delivery_id = response.json()["id"]
    finding_id = persist_report(auth_environment, team, delivery_id)
    path = (
        f"/api/projects/{team.project['id']}/deliveries/{delivery_id}/review"
        f"/findings/{finding_id}/annotation"
    )
    marked = team.other_member.post(
        path, json=annotation("not_adopted", "已与共同负责人核对本模块边界。"), headers=CSRF_HEADERS
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["actor_id"] == team.other_member_id
    response = auth_environment.make_client().get(
        f"/api/agent/deliveries/{delivery_id}/review", headers=bearer(team.token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"]["opinions"][0]["reason"] == "已与共同负责人核对本模块边界。"
    context = get_context(auth_environment, team, index=2)
    assert context["latest_review"]["content"]["opinions"][0]["actor_id"] == team.other_member_id


def test_human_routes_forward_only_authenticated_identity_and_preserve_report(
    auth_environment, review_team, monkeypatch
):
    team = review_team
    expected = report(team)
    calls = []

    def read(db, project_id, delivery_id, user_id):
        calls.append(("read", project_id, delivery_id, user_id))
        return deepcopy(expected)

    def mark(db, project_id, delivery_id, finding_id, user_id, **options):
        calls.append(("mark", project_id, delivery_id, finding_id, user_id, options))
        return {"finding_id": str(finding_id), "actor_id": str(user_id), **options}

    monkeypatch.setattr(review_reports, "get_review_report", read)
    monkeypatch.setattr(review_reports, "annotate_finding", mark)
    response = team.member.get(team.review_path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == expected
    response = team.member.post(
        team.annotation_path,
        json=annotation("not_adopted", "保留原结论，因为已有论据。", 2),
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["actor_id"] == team.member_id
    assert calls == [
        ("read", UUID(team.project["id"]), UUID(team.delivery["id"]), UUID(team.member_id)),
        (
            "mark",
            UUID(team.project["id"]),
            UUID(team.delivery["id"]),
            UUID(team.finding_id),
            UUID(team.member_id),
            {
                "handling": "not_adopted",
                "reason": "保留原结论，因为已有论据。",
                "expected_revision": 2,
            },
        ),
    ]


@pytest.mark.parametrize(
    "body",
    [
        annotation("not_adopted", ""),
        annotation("not_adopted", " \t\n "),
        annotation("adopted", "x" * 4001),
        annotation("not_adopted", "理由\x00正文"),
        annotation("unhandled"),
        annotation(revision=-1),
        annotation(revision=True),
        annotation(revision="0"),
        {"handling": "adopted"},
        {**annotation(), "actor_id": str(uuid4())},
        {**annotation(), "state": "adopted"},
    ],
)
def test_annotation_schema_rejects_missing_reason_revision_and_forged_fields(
    review_team, monkeypatch, body
):
    def must_not_run(*args, **kwargs):
        pytest.fail("Invalid annotation reached the service")

    monkeypatch.setattr(review_reports, "annotate_finding", must_not_run)
    response = review_team.member.post(review_team.annotation_path, json=body, headers=CSRF_HEADERS)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


def test_annotation_requires_human_session_and_same_origin_csrf(
    auth_environment, review_team, monkeypatch
):
    def must_not_run(*args, **kwargs):
        pytest.fail("Unauthenticated or cross-site request reached the service")

    monkeypatch.setattr(review_reports, "annotate_finding", must_not_run)
    team = review_team
    assert auth_environment.make_client().get(team.review_path).status_code == 401
    assert (
        auth_environment.make_client()
        .post(team.annotation_path, json=annotation(), headers=CSRF_HEADERS)
        .status_code
        == 401
    )
    for headers in ({}, {**CSRF_HEADERS, "Origin": "https://foreign.example"}):
        response = team.member.post(team.annotation_path, json=annotation(), headers=headers)
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (review_reports.ReportNotFoundError, 404),
        (review_reports.AnnotationPermissionError, 403),
        (review_reports.AnnotationConflictError, 409),
        (review_reports.AnnotationValidationError, 422),
        (projects.ProjectNotFoundError, 404),
    ],
)
def test_review_routes_map_fixed_errors_without_leaking_exception_text(
    review_team, monkeypatch, error, status
):
    def fail(*args, **kwargs):
        raise error("private-report-or-reason-must-not-leak")

    monkeypatch.setattr(review_reports, "get_review_report", fail)
    monkeypatch.setattr(review_reports, "annotate_finding", fail)
    for response in (
        review_team.member.get(review_team.review_path),
        review_team.member.post(
            review_team.annotation_path, json=annotation(), headers=CSRF_HEADERS
        ),
    ):
        assert response.status_code == status
        assert "private-report-or-reason-must-not-leak" not in response.text


@pytest.mark.parametrize("handling", ["adopted", "not_adopted"])
@pytest.mark.parametrize("owner_cookie", [False, True])
def test_agent_bearer_cannot_annotate_http_even_with_owner_cookie_and_is_audited(
    auth_environment, review_team, monkeypatch, handling, owner_cookie
):
    team = review_team

    def must_not_run(*args, **kwargs):
        pytest.fail("Agent crossed the human annotation boundary")

    monkeypatch.setattr(review_reports, "annotate_finding", must_not_run)
    client = team.member if owner_cookie else auth_environment.make_client()
    response = client.post(
        team.annotation_path,
        json=annotation(handling, "private-unadopted-reason-never-in-denial"),
        headers={**CSRF_HEADERS, **bearer(team.token)},
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    denials = recorded(auth_environment, team, "agent_permission_denied")
    assert len(denials) == 1
    event = denials[0]
    assert event.actor_type == "agent"
    assert event.actor_id == str(team.agent_id)
    assert event.represented_user_id == UUID(team.member_id)
    assert event.details["action"] == "annotate_review"
    assert "private-unadopted-reason-never-in-denial" not in json.dumps(event.details)


@pytest.mark.parametrize("owner_cookie", [False, True])
def test_agent_mcp_cannot_annotate_either_handling_and_retains_denial_audit(
    auth_environment, review_team, mcp_network, owner_cookie
):
    team = review_team
    headers = (
        {"Cookie": f"{COOKIE_NAME}={team.member.cookies.get(COOKIE_NAME)}"} if owner_cookie else {}
    )

    async def scenario():
        async with official_client(mcp_network, team.token, headers=headers) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "annotate_review" not in names
            for handling in ("adopted", "not_adopted"):
                result = await client.call_tool(
                    "annotate_review",
                    {
                        "project_id": team.project["id"],
                        "delivery_id": team.delivery["id"],
                        "finding_id": team.finding_id,
                        **annotation(handling, "private-MCP-rejection-reason"),
                    },
                )
                assert result.is_error
                serialized = json.dumps(result.model_dump(mode="json", by_alias=True))
                assert "permission_denied" in serialized
                assert "private-MCP-rejection-reason" not in serialized

    asyncio.run(scenario())
    calls = wait_for_records(auth_environment, team, "agent_tool_call", 2)
    assert len(calls) == 2
    assert all(row.details == {"tool": "unknown", "outcome": "error"} for row in calls)
    denials = recorded(auth_environment, team, "agent_permission_denied")
    assert len(denials) == 2
    assert all(row.details["action"] == "mcp_unknown_tool" for row in denials)
    assert all(row.actor_id == str(team.agent_id) for row in denials)


def test_agent_reads_real_report_data_and_task_context_only_for_own_delivery(
    auth_environment, review_team, monkeypatch
):
    team = review_team
    expected = report(team)
    seen = []

    def internal(db, item, *, viewer_owner_id):
        assert item.submitted_by == UUID(team.member_id)
        assert viewer_owner_id == UUID(team.member_id)
        seen.append(item.id)
        return deepcopy(expected)

    monkeypatch.setattr(review_reports, "get_report_internal", internal)
    response = auth_environment.make_client().get(
        f"/api/agent/deliveries/{team.delivery['id']}/review", headers=bearer(team.token)
    )
    assert response.status_code == 200
    assert response.json()["type"] == "untrusted_data"
    assert response.json()["content"] == expected
    context = get_context(auth_environment, team)
    assert context["latest_review"]["content"] == expected
    assert seen == [UUID(team.delivery["id"])] * 2
    assert recorded(auth_environment, team, "agent_review_read")[0].details == {
        "coverage": "completed"
    }
    assert (
        recorded(auth_environment, team, "agent_task_context_read")[-1].details["review_coverage"]
        == "completed"
    )
    with auth_environment.sessions() as db:
        events = db.scalars(select(Event).where(Event.actor_id == str(team.agent_id))).all()
        serialized = json.dumps([row.details for row in events], ensure_ascii=False)
        assert expected["opinions"][0]["reason"] not in serialized
        assert expected["opinions"][0]["opinion"]["problem"] not in serialized


def test_task_context_never_reads_other_submitter_report_on_shared_module(
    auth_environment, review_team, monkeypatch
):
    team = review_team
    response = upload_delivery(
        team.other_member, team, module_id=team.modules[2]["id"], content=b"other-report"
    )
    assert response.status_code == 201

    def must_not_run(*args, **kwargs):
        pytest.fail("Agent task context exposed another submitter's report")

    monkeypatch.setattr(review_reports, "get_report_internal", must_not_run)
    context = get_context(auth_environment, team, index=2)
    assert context["latest_review"]["content"]["coverage"] == "not_covered"
    assert context["latest_review"]["content"]["opinions"] == []
