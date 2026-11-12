"""Human report metadata uses real persisted permissions and preserves agent contracts."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from test_auth import register
from test_deliveries import ready_team, upload_delivery
from test_final_review import context, decide
from test_modules import publish_modules
from test_projects import create_project
from test_review_jobs import claim, database_time, fail
from test_review_reports import annotate, components, opinion, process, submit

from app.models import DeliveryVersion, DeliveryWorkflow, ProjectMember
from app.schemas.review_jobs import ReviewProgress
from app.services import review_reports

HUMAN_METADATA = {"can_annotate", "can_retry", "workflow_revision", "review_progress"}
SOURCE_METADATA = {"delivery_id", "delivery_version"}


def view(team, delivery, *, client=None):
    response = (client or team.member).get(
        f"/api/projects/{team.project['id']}/deliveries/{delivery['id']}/review"
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def assert_workflow_metadata(environment, delivery, result):
    with environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(delivery["id"]))
        workflow = db.get(DeliveryWorkflow, item.id)
        assert result["workflow_revision"] == (workflow.revision if workflow else None)
        assert result["review_progress"] == (
            ReviewProgress.model_validate(item.review_progress).model_dump(mode="json")
            if item.review_progress
            else None
        )
    assert result["can_retry"] is False


def test_uncovered_human_view_exposes_real_queue_without_annotation_permission(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    result = view(team, delivery)
    assert result["coverage"] == "not_covered"
    assert result["opinions"] == [] and not result["can_annotate"]
    assert result["workflow_revision"] == 1
    assert result["review_progress"]["status"] == "queued"
    assert_workflow_metadata(auth_environment, delivery, result)


@pytest.mark.parametrize("severity", ["important", "suggestion"])
def test_current_opinion_has_delivery_origin_and_only_actual_assignees_can_annotate(
    auth_environment, severity
):
    team = ready_team(auth_environment, multiple_owners=True)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion(severity)))
    for client in (team.member, team.other_member):
        result = view(team, delivery, client=client)
        assert result["can_annotate"] is True
        assert result["opinions"][0]["delivery_id"] == delivery["id"]
        assert result["opinions"][0]["delivery_version"] == delivery["version"]
        assert_workflow_metadata(auth_environment, delivery, result)
    assert view(team, delivery, client=team.leader)["can_annotate"] is False


def test_current_plan_reassignment_and_module_removal_preserve_submitter_responsibility(
    auth_environment,
):
    team = ready_team(auth_environment, multiple_owners=True)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion()))
    assert view(team, delivery, client=team.other_member)["can_annotate"] is True
    modules = deepcopy(team.modules)
    modules[0]["assignee_ids"] = [team.leader_id]
    publish_modules(team, modules, "移交模块")
    assert view(team, delivery, client=team.other_member)["can_annotate"] is False
    assert view(team, delivery, client=team.leader)["can_annotate"] is True
    assert view(team, delivery)["can_annotate"] is True

    modules = modules[1:]
    modules[0]["upstream"] = []
    publish_modules(team, modules, "移除模块并关闭旧依赖")
    assert view(team, delivery, client=team.leader)["can_annotate"] is False
    assert view(team, delivery, client=team.other_member)["can_annotate"] is False
    assert view(team, delivery)["can_annotate"] is True


def test_incomplete_report_keeps_progress_and_does_not_allow_annotation(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion(), failure="specification"))
    result = view(team, delivery)
    assert result["coverage"] == "incomplete"
    assert result["state"] == "ai_review_partial_failure"
    assert result["can_annotate"] is False
    assert result["review_progress"]["status"] == "retry_wait"
    assert_workflow_metadata(auth_environment, delivery, result)


def test_exhausted_failure_retry_capability_follows_current_module_authority(auth_environment):
    team = ready_team(auth_environment, multiple_owners=True)
    delivery = submit(auth_environment, team)
    now = database_time(auth_environment)
    claimed = claim(auth_environment, now, max_attempts=1)
    assert claimed is not None and fail(auth_environment, claimed, now)
    for client in (team.member, team.other_member):
        result = view(team, delivery, client=client)
        assert result["review_progress"]["status"] == "failed"
        assert result["can_retry"] is True and result["can_annotate"] is False
    assert view(team, delivery, client=team.leader)["can_retry"] is False
    modules = deepcopy(team.modules)
    modules[0]["assignee_ids"] = [team.leader_id]
    publish_modules(team, modules, "移交失败审核的模块")
    assert view(team, delivery, client=team.other_member)["can_retry"] is False
    assert view(team, delivery, client=team.leader)["can_retry"] is True
    assert view(team, delivery)["can_retry"] is True
    modules = modules[1:]
    modules[0]["upstream"] = []
    publish_modules(team, modules, "移除模块，提交人仍负责恢复审核")
    assert view(team, delivery, client=team.leader)["can_retry"] is False
    assert view(team, delivery)["can_retry"] is True


def test_loop_bypassed_report_has_history_sources_but_no_writable_current_opinion(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion()))
    team.member.app.state.settings.review_return_limit = 1
    bypassed = submit(auth_environment, team)
    result = view(team, bypassed)
    assert result["coverage"] == "bypassed" and result["nonconverged"]
    assert result["can_annotate"] is False and result["opinions"] == []
    assert result["review_progress"] is None
    assert result["history"][0]["delivery_id"] == delivery["id"]
    assert result["history"][0]["delivery_version"] == 1
    assert_workflow_metadata(auth_environment, bypassed, result)


@pytest.mark.parametrize("decision", ["approve", "return"])
def test_terminal_final_review_disables_annotation_but_preserves_human_report(
    auth_environment, decision
):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion("suggestion")))
    assert view(team, delivery)["can_annotate"] is True
    snapshot = context(auth_environment, team, delivery)
    decide(
        auth_environment,
        team,
        delivery,
        snapshot=snapshot,
        decision=decision,
        comment="负责人已核对当前意见。",
    )
    result = view(team, delivery)
    assert result["can_annotate"] is False
    assert result["state"] == ("adopted" if decision == "approve" else "final_returned")
    assert result["final_review"]["requested_decision"] == decision
    assert result["opinions"][0]["delivery_id"] == delivery["id"]
    assert_workflow_metadata(auth_environment, delivery, result)


def test_legacy_delivery_without_workflow_has_explicit_null_revision_and_progress(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    with auth_environment.sessions() as db:
        original = db.get(DeliveryVersion, UUID(delivery["id"]))
        fields = {
            column.key: getattr(original, column.key)
            for column in DeliveryVersion.__table__.columns
            if column.key not in {"id", "version", "submitted_at"}
        }
        legacy = DeliveryVersion(**fields, version=original.version + 1)
        db.add(legacy)
        db.commit()
        legacy_id = str(legacy.id)
    result = view(team, {"id": legacy_id})
    assert result["coverage"] == "not_covered"
    assert result["workflow_revision"] is None and result["review_progress"] is None
    assert result["can_annotate"] is False and result["can_retry"] is False


def test_human_current_and_history_sources_do_not_change_internal_or_agent_filtered_contract(
    auth_environment,
):
    team = ready_team(auth_environment, multiple_owners=True)
    old = submit(auth_environment, team)
    old_report, _, _ = process(auth_environment, team, old, components(opinion()))
    old_finding = old_report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, old, old_finding)
    response = upload_delivery(team.other_member, team, b"shared module revision")
    assert response.status_code == 201, response.text
    new = response.json()
    process(auth_environment, team, new, components(opinion()))
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(new["id"]))
        internal = review_reports.get_report_internal(db, item)
        filtered = review_reports.get_report_internal(
            db, item, viewer_owner_id=UUID(team.other_member_id)
        )
        result = review_reports.get_review_report(
            db, UUID(team.project["id"]), item.id, UUID(team.other_member_id)
        )
        assert review_reports.get_report_internal(db, item) == internal
        assert (
            review_reports.get_report_internal(db, item, viewer_owner_id=UUID(team.other_member_id))
            == filtered
        )
    assert HUMAN_METADATA <= result.keys()
    assert HUMAN_METADATA.isdisjoint(internal) and HUMAN_METADATA.isdisjoint(filtered)
    assert all(
        SOURCE_METADATA.isdisjoint(finding)
        for report in (internal, filtered)
        for finding in [*report["opinions"], *report.get("history", [])]
    )
    assert result["opinions"][0]["delivery_id"] == new["id"]
    assert result["opinions"][0]["delivery_version"] == 2
    assert result["history"][0]["delivery_id"] == old["id"]
    assert result["history"][0]["delivery_version"] == 1
    assert result["opinions"][0]["duplicate_of"] == old_finding
    assert result["can_annotate"] is False
    assert filtered["history"] == [] and filtered["opinions"][0]["reason"] == ""


def test_report_view_does_not_expose_capabilities_to_nonmember_or_wrong_project(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion()))
    outsider = auth_environment.make_client()
    assert register(outsider, "report-outsider").status_code == 201
    foreign = create_project(outsider)
    path = f"/api/projects/{team.project['id']}/deliveries/{delivery['id']}/review"
    assert outsider.get(path).status_code == 404
    assert auth_environment.make_client().get(path).status_code == 401
    assert (
        outsider.get(
            f"/api/projects/{foreign['id']}/deliveries/{delivery['id']}/review"
        ).status_code
        == 404
    )
    assert (
        team.member.get(
            f"/api/projects/{team.project['id']}/deliveries/{uuid4()}/review"
        ).status_code
        == 404
    )


def test_former_submitter_losing_project_membership_cannot_read_or_receive_capabilities(
    auth_environment,
):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion()))
    assert view(team, delivery)["can_annotate"] is True
    with auth_environment.sessions() as db:
        db.delete(db.get(ProjectMember, (UUID(team.project["id"]), UUID(team.member_id))))
        db.commit()
    response = team.member.get(
        f"/api/projects/{team.project['id']}/deliveries/{delivery['id']}/review"
    )
    assert response.status_code == 404
    assert HUMAN_METADATA.isdisjoint(response.json())
