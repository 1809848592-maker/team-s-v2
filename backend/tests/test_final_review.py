"""M3-11 项目负责人的终审权限、意见快照、原子性与并发，使用独立PG。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_deliveries import ready_team, upload_delivery
from test_modules import publish_modules
from test_review_reports import annotate, components, opinion, process, submit

from app.models import DeliveryVersion, DeliveryWorkflow, Event, FinalReviewDecision, ProjectMember
from app.models.immutable import SnapshotImmutableError
from app.models.review_report import ReviewFinding, ReviewSummary
from app.services import final_review, review_reports
from app.services.projects import ProjectNotFoundError


def context(environment, team, delivery, user_id=None):
    with environment.sessions() as db:
        return final_review.get_final_review(
            db, UUID(team.project["id"]), UUID(delivery["id"]), UUID(user_id or team.leader_id)
        )


def decide(environment, team, delivery, *, user_id=None, snapshot=None, **changes):
    snapshot = snapshot if snapshot is not None else context(environment, team, delivery, user_id)
    body = dict(
        decision="approve",
        expected_revision=snapshot["workflow_revision"],
        context_hash=snapshot["context_hash"],
    )
    body.update(changes)
    with environment.sessions() as db:
        return final_review.decide_final_review(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            UUID(user_id or team.leader_id),
            body,
        )


def pending(environment, *, multiple_owners=False, findings=()):
    team = ready_team(environment, multiple_owners=multiple_owners)
    delivery = submit(environment, team)
    report, _, _ = process(environment, team, delivery, components(*findings))
    assert report["state"] == "pending_final_review"
    return team, delivery


def add_leader(environment, team, identity):
    with environment.sessions() as db:
        member = db.get(ProjectMember, (UUID(team.project["id"]), UUID(identity)))
        member.role = "leader"
        db.commit()


def self_pending(environment, *, two_leaders=False):
    team = ready_team(environment)
    modules = deepcopy(team.modules)
    modules[0]["assignee_ids"] = [team.leader_id]
    publish_modules(team, modules, "负责人承担模块")
    team.modules = modules
    delivery = upload_delivery(team.leader, team).json()
    process(environment, team, delivery)
    if two_leaders:
        add_leader(environment, team, team.other_member_id)
    return team, delivery


@pytest.mark.parametrize("decision,outcome", [("approve", "adopted"), ("return", "final_returned")])
def test_one_project_leader_decides_and_records_human_snapshot(auth_environment, decision, outcome):
    team, delivery = pending(auth_environment)
    old = context(auth_environment, team, delivery)
    assert old["can_decide"] and not old["self_review"]
    result = decide(
        auth_environment, team, delivery, decision=decision, comment="负责人核对后的意见"
    )
    assert result["outcome"] == outcome and not result["self_review"]
    with auth_environment.sessions() as db:
        record = db.scalar(select(FinalReviewDecision))
        assert record.snapshot["submitted_by"] == team.member_id
        assert record.snapshot["spec_version_id"] == delivery["spec_version_id"]
        assert record.snapshot["plan_version_id"] == delivery["plan_version_id"]
        assert record.context_hash == old["context_hash"]
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == outcome
        event = db.get(Event, record.event_id)
        assert event.actor_type == "human" and event.actor_id == team.leader_id
        assert "负责人核对后的意见" not in str(event.details)
        report = review_reports.get_report_internal(
            db, db.get(DeliveryVersion, UUID(delivery["id"]))
        )
        assert report["final_review"]["comment"] == "负责人核对后的意见"
    after = context(auth_environment, team, delivery)
    assert not after["can_decide"] and after["decision"]["id"] == result["id"]
    assert after["context_hash"] == old["context_hash"]
    assert after["opinions"] == old["opinions"]


@pytest.mark.parametrize("decision", ["approve", "return"])
def test_nonleaders_and_module_coowners_cannot_final_review(auth_environment, decision):
    team, delivery = pending(auth_environment, multiple_owners=True)
    for identity in (team.member_id, team.other_member_id):
        snapshot = context(auth_environment, team, delivery, identity)
        assert not snapshot["can_decide"]
        with pytest.raises(final_review.FinalReviewPermissionError):
            decide(
                auth_environment,
                team,
                delivery,
                user_id=identity,
                decision=decision,
                comment="退回意见",
            )
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(FinalReviewDecision))) == []


@pytest.mark.parametrize("decision", ["approve", "return"])
def test_sole_leader_may_review_own_delivery_and_is_marked(auth_environment, decision):
    team, delivery = self_pending(auth_environment)
    assert context(auth_environment, team, delivery)["self_review"]
    result = decide(auth_environment, team, delivery, decision=decision, comment="本人核验")
    assert result["self_review"]
    with auth_environment.sessions() as db:
        assert db.scalar(select(FinalReviewDecision.self_review))
        assert db.scalar(
            select(Event).where(Event.event_type == "delivery_final_reviewed")
        ).details["self_review"]


@pytest.mark.parametrize("decision", ["approve", "return"])
def test_multiple_leaders_prevent_self_review_and_other_leader_can_decide(
    auth_environment, decision
):
    team, delivery = self_pending(auth_environment, two_leaders=True)
    assert not context(auth_environment, team, delivery)["can_decide"]
    with pytest.raises(final_review.FinalReviewPermissionError):
        decide(auth_environment, team, delivery, decision=decision, comment="自己提交")
    result = decide(
        auth_environment,
        team,
        delivery,
        user_id=team.other_member_id,
        decision=decision,
        comment="另一负责人核验",
    )
    assert not result["self_review"]


def test_second_leader_join_makes_previous_self_review_context_stale(auth_environment):
    team, delivery = self_pending(auth_environment)
    snapshot = context(auth_environment, team, delivery)
    add_leader(auth_environment, team, team.other_member_id)
    with pytest.raises(final_review.FinalReviewPermissionError):
        decide(auth_environment, team, delivery, snapshot=snapshot)


def test_any_non_submitting_leader_can_approve_without_other_votes(auth_environment):
    team, delivery = pending(auth_environment)
    add_leader(auth_environment, team, team.other_member_id)
    result = decide(auth_environment, team, delivery, user_id=team.other_member_id)
    assert result["outcome"] == "adopted"
    with pytest.raises(final_review.FinalReviewConflictError):
        decide(auth_environment, team, delivery)


def test_concurrent_leader_decisions_only_one_commits(auth_environment):
    team, delivery = pending(auth_environment)
    add_leader(auth_environment, team, team.other_member_id)
    old = context(auth_environment, team, delivery)

    def finish(identity):
        try:
            return decide(auth_environment, team, delivery, user_id=identity, snapshot=old)[
                "outcome"
            ]
        except final_review.FinalReviewConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, (team.leader_id, team.other_member_id)))
    assert sorted(results) == ["adopted", "conflict"]
    with auth_environment.sessions() as db:
        assert len(list(db.scalars(select(FinalReviewDecision)))) == 1
        assert (
            len(
                list(db.scalars(select(Event).where(Event.event_type == "delivery_final_reviewed")))
            )
            == 1
        )


def spec_pending(environment):
    team, delivery = pending(
        environment,
        findings=(
            opinion("suggestion", category="specification", reviewer="spec_compliance"),
            opinion("suggestion"),
        ),
    )
    with environment.sessions() as db:
        report = review_reports.get_report_internal(
            db, db.get(DeliveryVersion, UUID(delivery["id"]))
        )
    for entry in report["opinions"]:
        annotate(environment, team, delivery, entry["finding_id"], reason="适用范围不同，不采纳。")
    return team, delivery


@pytest.mark.parametrize("ruling,outcome", [("accept", "adopted"), ("reject", "final_returned")])
def test_specification_exception_requires_explicit_ruling_and_reject_returns(
    auth_environment, ruling, outcome
):
    team, delivery = spec_pending(auth_environment)
    snapshot = context(auth_environment, team, delivery)
    assert len(snapshot["not_adopted"]) == 2 and len(snapshot["specification_exceptions"]) == 1
    finding = snapshot["specification_exceptions"][0]["finding_id"]
    with pytest.raises(final_review.FinalReviewValidationError):
        decide(auth_environment, team, delivery)
    result = decide(
        auth_environment,
        team,
        delivery,
        spec_rulings=[
            {
                "finding_id": finding,
                "decision": ruling,
                "comment": "负责人要求按规范补充" if ruling == "reject" else "范围确认",
            }
        ],
    )
    assert result["outcome"] == outcome
    assert result["spec_rulings"][0]["decision"] == ruling


def test_unknown_and_duplicate_spec_rulings_rejected_without_mutation(auth_environment):
    team, delivery = spec_pending(auth_environment)
    with pytest.raises(final_review.FinalReviewValidationError):
        decide(
            auth_environment,
            team,
            delivery,
            spec_rulings=[{"finding_id": str(uuid4()), "decision": "accept"}],
        )
    snapshot = context(auth_environment, team, delivery)
    ruling = {
        "finding_id": snapshot["specification_exceptions"][0]["finding_id"],
        "decision": "accept",
    }
    with pytest.raises(final_review.FinalReviewValidationError):
        decide(auth_environment, team, delivery, spec_rulings=[ruling, ruling])
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(FinalReviewDecision))) == []


def test_context_hash_rejects_annotation_edit_without_workflow_change(auth_environment):
    team, delivery = spec_pending(auth_environment)
    old = context(auth_environment, team, delivery)
    entry = old["specification_exceptions"][0]
    annotate(
        auth_environment, team, delivery, entry["finding_id"], reason="更新后的证据理由", revision=1
    )
    new = context(auth_environment, team, delivery)
    assert old["workflow_revision"] == new["workflow_revision"]
    assert old["context_hash"] != new["context_hash"]
    with pytest.raises(final_review.FinalReviewConflictError):
        decide(auth_environment, team, delivery, snapshot=old)
    result = decide(
        auth_environment,
        team,
        delivery,
        spec_rulings=[{"finding_id": entry["finding_id"], "decision": "accept"}],
    )
    with auth_environment.sessions() as db:
        assert (
            db.scalar(select(FinalReviewDecision)).snapshot["specification_exceptions"][0]["reason"]
            == "更新后的证据理由"
        )
        assert db.scalar(select(FinalReviewDecision)).id == UUID(result["id"])


@pytest.mark.parametrize(
    "state", ["ai_reviewing", "ai_review_partial_failure", "returned", "adopted", "final_returned"]
)
def test_only_pending_final_state_is_eligible(auth_environment, state):
    team, delivery = pending(auth_environment)
    old = context(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        workflow = db.get(DeliveryWorkflow, UUID(delivery["id"]))
        workflow.state = state
        db.commit()
    with pytest.raises(final_review.FinalReviewConflictError):
        decide(auth_environment, team, delivery, snapshot=old)


def test_no_cross_project_or_unknown_delivery_access(auth_environment):
    team, delivery = pending(auth_environment)
    with auth_environment.sessions() as db:
        for project, target, user in [
            (uuid4(), UUID(delivery["id"]), UUID(team.leader_id)),
            (UUID(team.project["id"]), uuid4(), UUID(team.leader_id)),
        ]:
            with pytest.raises((ProjectNotFoundError, final_review.FinalReviewNotFoundError)):
                final_review.get_final_review(db, project, target, user)
            db.rollback()


def test_return_requires_comment_and_all_decision_records_are_immutable(auth_environment):
    team, delivery = pending(auth_environment)
    with pytest.raises(final_review.FinalReviewValidationError):
        decide(auth_environment, team, delivery, decision="return", comment=" ")
    decide(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        record = db.scalar(select(FinalReviewDecision))
        record.comment = "cannot mutate"
        with pytest.raises(SnapshotImmutableError):
            db.flush()
        db.rollback()
        schema = db.get_bind().get_execution_options()["schema_translate_map"][None]
        with pytest.raises(DBAPIError) as error:
            db.execute(text(f'UPDATE "{schema}".final_review_decisions SET comment=comment'))
        assert error.value.orig.sqlstate == "55000"
        db.rollback()


def test_late_event_failure_rolls_back_decision_and_adoption(auth_environment, monkeypatch):
    team, delivery = pending(auth_environment)
    original = final_review.record_event

    def fail(db, **values):
        original(db, **values)
        raise RuntimeError("fixed late event failure")

    with monkeypatch.context() as patch:
        patch.setattr(final_review, "record_event", fail)
        with pytest.raises(RuntimeError):
            decide(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(FinalReviewDecision))) == []
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "pending_final_review"
        assert (
            list(db.scalars(select(Event).where(Event.event_type == "delivery_final_reviewed")))
            == []
        )
    assert decide(auth_environment, team, delivery)["outcome"] == "adopted"


def test_return_resubmits_as_new_version_without_rewriting_old_decision(auth_environment):
    team, delivery = pending(auth_environment)
    original = decide(auth_environment, team, delivery, decision="return", comment="请补论据")
    new = submit(auth_environment, team)
    assert new["version"] == 2 and new["review_status"] == "ai_reviewing"
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, UUID(delivery["id"])).state == "final_returned"
        assert db.scalar(select(FinalReviewDecision)).id == UUID(original["id"])


def test_bypassed_round_can_final_review_with_unhandled_history(auth_environment):
    team = ready_team(auth_environment)
    for _ in range(3):
        old = submit(auth_environment, team)
        process(auth_environment, team, old, components(opinion()))
    delivery = submit(auth_environment, team)
    snapshot = context(auth_environment, team, delivery)
    assert snapshot["nonconverged"] and len(snapshot["history"]) == 3
    assert decide(auth_environment, team, delivery)["outcome"] == "adopted"


def test_final_context_never_silently_drops_history_over_report_page_limit(auth_environment):
    team, old = pending(auth_environment)
    with auth_environment.sessions() as db:
        summary = db.scalar(select(ReviewSummary))
        for _ in range(501):
            db.add(ReviewFinding(summary_id=summary.id, opinion=opinion("suggestion")))
        db.commit()
    # 达循环上限可进入终审；不让自动模型审核超限被当成通过。
    for index in range(3):
        with auth_environment.sessions() as db:
            if index == 0:
                delivery = db.get(DeliveryVersion, UUID(old["id"]))
            else:
                latest = submit(auth_environment, team)
                delivery = db.get(DeliveryVersion, UUID(latest["id"]))
            workflow = db.get(DeliveryWorkflow, delivery.id)
            workflow.state = "returned"
            db.commit()
    latest = submit(auth_environment, team)
    snapshot = context(auth_environment, team, latest)
    assert len(snapshot["history"]) == 501
    assert decide(auth_environment, team, latest)["outcome"] == "adopted"


def test_final_context_overflow_fails_closed(auth_environment, monkeypatch):
    team, delivery = pending(auth_environment, findings=(opinion("suggestion"),))
    monkeypatch.setattr(final_review, "MAX_FINAL_FINDINGS", 0)
    with pytest.raises(final_review.FinalReviewValidationError):
        context(auth_environment, team, delivery)


def test_duplicate_spec_exception_is_grouped_with_original_handling(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team)
    report, _, _ = process(
        auth_environment,
        team,
        first,
        components(opinion(category="specification", reviewer="spec_compliance")),
    )
    original_id = report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, original_id, reason="原规范不适用理由")
    second = submit(auth_environment, team)
    report, _, _ = process(
        auth_environment,
        team,
        second,
        components(opinion(category="specification", reviewer="spec_compliance")),
    )
    assert report["state"] == "pending_final_review"
    current_id = report["opinions"][0]["finding_id"]
    snapshot = context(auth_environment, team, second)
    assert len(snapshot["not_adopted"]) == len(snapshot["specification_exceptions"]) == 1
    entry = snapshot["specification_exceptions"][0]
    assert entry["finding_id"] == current_id
    assert set(entry["related_finding_ids"]) == {original_id, current_id}
    assert entry["reason"] == "原规范不适用理由"
    decide(
        auth_environment,
        team,
        second,
        spec_rulings=[{"finding_id": current_id, "decision": "accept"}],
    )
    # 原退回版本仍可追加标注；已终审快照不能被改写。
    annotate(auth_environment, team, first, original_id, reason="后来补充的新理由", revision=1)
    final = context(auth_environment, team, second)
    assert final["specification_exceptions"][0]["reason"] == "原规范不适用理由"
    assert final["context_hash"] == snapshot["context_hash"]


def test_agent_receives_own_final_comment_but_not_foreign_spec_ruling(auth_environment):
    team = ready_team(auth_environment, multiple_owners=True)
    first = submit(auth_environment, team)
    report, _, _ = process(
        auth_environment,
        team,
        first,
        components(opinion(category="specification", reviewer="spec_compliance")),
    )
    old_id = report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, old_id, reason="FOREIGN_ANNOTATION_REASON")
    # 新提交人没有重命中；旧不采纳项必须给负责人看，但不能经新提交人agent外泄。
    second = upload_delivery(team.other_member, team, b"new source").json()
    process(auth_environment, team, second)
    snap = context(auth_environment, team, second)
    assert len(snap["specification_exceptions"]) == 1
    decide(
        auth_environment,
        team,
        second,
        comment="已核对本次交付",
        spec_rulings=[
            {"finding_id": old_id, "decision": "accept", "comment": "FOREIGN_RULING_COMMENT"}
        ],
    )
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(second["id"]))
        agent_report = review_reports.get_report_internal(
            db, item, viewer_owner_id=UUID(team.other_member_id)
        )
        assert agent_report["final_review"]["comment"] == "已核对本次交付"
        assert agent_report["final_review"]["spec_rulings"] == []
        assert "FOREIGN_RULING_COMMENT" not in str(agent_report)
        assert "FOREIGN_ANNOTATION_REASON" not in str(agent_report)


def test_final_context_detects_changed_historical_annotation(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, first, components(opinion()))
    old_id = report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, old_id)
    second = submit(auth_environment, team)
    process(auth_environment, team, second)
    old = context(auth_environment, team, second)
    annotate(auth_environment, team, first, old_id, reason="历史处理追加依据", revision=1)
    with pytest.raises(final_review.FinalReviewConflictError):
        decide(auth_environment, team, second, snapshot=old)
    assert decide(auth_environment, team, second)["outcome"] == "adopted"


def test_final_context_rejects_missing_summary_stale_revision_and_too_large_snapshot(
    auth_environment, monkeypatch
):
    team, delivery = pending(auth_environment)
    snap = context(auth_environment, team, delivery)
    with pytest.raises(final_review.FinalReviewConflictError):
        decide(auth_environment, team, delivery, expected_revision=snap["workflow_revision"] + 1)
    with monkeypatch.context() as patch:
        patch.setattr(final_review, "MAX_SNAPSHOT_BYTES", 1)
        with pytest.raises(final_review.FinalReviewValidationError):
            context(auth_environment, team, delivery)
    new = submit(auth_environment, team)
    with auth_environment.sessions() as db:
        workflow = db.get(DeliveryWorkflow, UUID(new["id"]))
        workflow.state = "pending_final_review"
        db.commit()
    with pytest.raises(final_review.FinalReviewConflictError):
        context(auth_environment, team, new)


def test_reject_return_and_spec_ruling_inputs_do_not_allow_bad_data(auth_environment):
    team, delivery = spec_pending(auth_environment)
    snap = context(auth_environment, team, delivery)
    identity = snap["specification_exceptions"][0]["finding_id"]
    for changes in (
        dict(decision="approve", actor_id=team.member_id),
        dict(comment="bad\x00data"),
        dict(spec_rulings=[{"finding_id": identity, "decision": "reject", "comment": " "}]),
        dict(expected_revision=True),
        dict(context_hash="invalid"),
    ):
        with pytest.raises(final_review.FinalReviewValidationError):
            decide(auth_environment, team, delivery, **changes)
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(FinalReviewDecision))) == []
