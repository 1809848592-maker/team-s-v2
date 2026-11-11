"""M3-10 汇总、不可变记录、历史匹配和循环上限的真实PostgreSQL测试。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_deliveries import ready_team, upload_delivery
from test_review_jobs import claim, database_time, job_for

from app.models import DeliveryVersion, DeliveryWorkflow, Event, ReviewJob
from app.models.review_report import ReviewAnnotation, ReviewFinding, ReviewReport, ReviewSummary
from app.review.runner import DeliveryReviewInput
from app.services import review_reports as reports
from app.services.review_jobs import complete_job


def opinion(severity="important", **changes):
    value = dict(
        category="logic",
        severity=severity,
        location={"description": "正文", "part": "document", "paragraph": 1},
        problem="该论证缺乏支持核心结论所需的有效证据。",
        basis={"excerpt": "证据"},
        suggestion="补充证据",
        finding_id=str(uuid4()),
        reviewer="logic",
        instruction_version="2026-11-12.1",
        model="fixed-test",
        input_hash="a" * 64,
        instruction_hash="b" * 64,
        call_id=str(uuid4()),
    )
    value.update(changes)
    return value


def components(*findings, failure=None):
    completed = {"status": "completed", "findings": []}
    value = {
        "format": {"status": "partial", "findings": [], "coverage": ["未覆盖"]},
        "specification": {"status": "no_ai_items", "review": None},
        "citations": {
            "status": "completed",
            "extraction": deepcopy(completed),
            "alignment": deepcopy(completed),
            "database_findings": [],
        },
        "reasoning": {
            "status": "completed",
            "logic": {**completed, "findings": list(findings)},
            "linkage": deepcopy(completed),
        },
    }
    if failure:
        value[failure] = {"status": "incomplete", "error_code": "fixed_failure"}
    return value


def submit(environment, team):
    result = upload_delivery(team.member, team, b"proof")
    assert result.status_code == 201, result.text
    return result.json()


def process(environment, team, delivery, data=None):
    claimed = claim(environment, database_time(environment))
    assert claimed is not None
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    reference = reports.stage_report(
        environment.sessions, review, data if data is not None else components()
    )
    with environment.sessions() as db:
        assert complete_job(db, claimed, reference)
        db.commit()
        item = db.get(DeliveryVersion, UUID(delivery["id"]))
        result = reports.get_report_internal(db, item)
    return result, claimed, reference


def annotate(
    environment,
    team,
    delivery,
    finding,
    handling="not_adopted",
    reason="已有独立论据，保留。",
    revision=0,
):
    with environment.sessions() as db:
        return reports.annotate_finding(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            UUID(finding),
            UUID(team.member_id),
            handling=handling,
            reason=reason,
            expected_revision=revision,
        )


@pytest.mark.parametrize(
    "severity,state",
    [
        ("blocking", "returned"),
        ("important", "returned"),
        ("suggestion", "pending_final_review"),
        (None, "pending_final_review"),
    ],
)
def test_severity_decision_and_full_report_reference(auth_environment, severity, state):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    data = components(*([opinion(severity)] if severity else []))
    data["format"]["coverage"].append("x" * 70000)
    result, claimed, reference = process(auth_environment, team, delivery, data)
    assert result["state"] == state and result["coverage"] == "completed"
    assert result["components"] == data and len(str(reference)) < 100
    with auth_environment.sessions() as db:
        assert job_for(db, delivery).result == reference
        assert len(list(db.scalars(select(ReviewSummary)))) == 1
        assert not complete_job(db, claimed, reference)
        assert db.get(DeliveryVersion, claimed.delivery_id).sha256 == delivery["sha256"]


@pytest.mark.parametrize("stage", ["format", "specification", "citations", "reasoning"])
def test_any_incomplete_preserves_report_without_approval(auth_environment, stage):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    result, claimed, reference = process(
        auth_environment, team, delivery, components(opinion(), failure=stage)
    )
    assert result["coverage"] == "incomplete" and result["state"] == "ai_review_partial_failure"
    with auth_environment.sessions() as db:
        assert job_for(db, delivery).status == "retry_wait"
        assert job_for(db, delivery).result == reference
        assert reports.return_count(db, db.get(DeliveryVersion, claimed.delivery_id)) == 0


def test_rejected_duplicate_is_linked_and_does_not_block_but_adopted_reappears(auth_environment):
    team = ready_team(auth_environment)
    old = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, old, components(opinion()))
    finding = result["opinions"][0]["finding_id"]
    annotate(auth_environment, team, old, finding)
    new = submit(auth_environment, team)
    result, _, _ = process(
        auth_environment,
        team,
        new,
        components(opinion(problem="该论证缺乏支持核心结论所需的有效证据！")),
    )
    assert result["state"] == "pending_final_review"
    assert result["opinions"][0]["duplicate_of"] == finding
    assert result["opinions"][0]["reason"] == "已有独立论据，保留。"
    annotate(auth_environment, team, old, finding, handling="adopted", reason="已修改", revision=1)
    third = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, third, components(opinion()))
    assert result["state"] == "returned" and result["opinions"][0]["duplicate_of"] is None


def test_unhandled_old_important_still_blocks_but_old_suggestion_is_optional(auth_environment):
    team = ready_team(auth_environment)
    old = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, old, components(opinion()))
    new = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, new)
    assert result["state"] == "returned"
    assert result["history"][0]["handling"] == "unhandled"


@pytest.mark.parametrize(
    "changes",
    [
        dict(category="linkage"),
        dict(location={"description": "正文", "part": "document", "paragraph": 2}),
        dict(problem="研究设计中的样本选择与识别策略存在完全不同的问题。"),
        dict(basis={"spec_number": "A2", "excerpt": "证据"}),
    ],
)
def test_different_location_category_or_problem_does_not_match(changes):
    assert not reports.same_problem(opinion(), opinion(**changes))


def test_similarity_is_normalized_and_citation_excerpt_is_not_merged():
    assert reports.same_problem(
        opinion(), opinion(problem="该论证缺乏支持核心结论所需的有效证据！")
    )
    assert not reports.same_problem(
        opinion(category="citation", basis={"excerpt": "文献A"}),
        opinion(category="citation", basis={"excerpt": "文献B"}),
    )


def test_loop_limit_creates_unconverged_fourth_round_without_job(auth_environment):
    team = ready_team(auth_environment)
    for index in range(3):
        delivery = submit(auth_environment, team)
        result, _, _ = process(auth_environment, team, delivery, components(opinion()))
        assert result["state"] == "returned" and result["round"] == index + 1
    fourth = submit(auth_environment, team)
    assert fourth["review_status"] == "pending_final_review"
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(fourth["id"]))
        assert job_for(db, fourth) is None
        report = reports.get_report_internal(db, item)
        assert report["nonconverged"] and report["round"] == 4
        assert "未收敛" in report["note"]
        assert len(list(db.scalars(select(ReviewJob)))) == 3


def test_configured_return_limit_and_owner_confirmation_path(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    process(auth_environment, team, delivery, components(opinion()))
    team.member.app.state.settings.review_return_limit = 1
    next_delivery = submit(auth_environment, team)
    assert next_delivery["review_status"] == "pending_final_review"


def test_annotation_reason_conflict_and_concurrent_append_are_atomic(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, delivery, components(opinion()))
    finding = result["opinions"][0]["finding_id"]
    with pytest.raises(reports.AnnotationValidationError):
        annotate(auth_environment, team, delivery, finding, reason="  ")

    def write():
        try:
            return annotate(auth_environment, team, delivery, finding)["annotation_revision"]
        except reports.AnnotationConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _: write(), range(2)))
    assert set(values) == {1, "conflict"}
    with auth_environment.sessions() as db:
        assert db.scalar(select(func.count()).select_from(ReviewAnnotation)) == 1
        events = list(
            db.scalars(select(Event).where(Event.event_type == "review_finding_annotated"))
        )
        assert len(events) == 1 and "reason" not in events[0].details


def test_stale_forged_or_foreign_report_never_changes_workflow(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    claimed = claim(auth_environment, database_time(auth_environment))
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    for bad in (
        replace(review, project_id=uuid4()),
        replace(review, attempt=2),
        replace(review, workflow_revision=99),
    ):
        with pytest.raises(reports.ReportValidationError):
            reports.stage_report(auth_environment.sessions, bad, components())
    reference = reports.stage_report(auth_environment.sessions, review, components(opinion()))
    with auth_environment.sessions() as db:
        assert not complete_job(db, replace(claimed, claim_token=uuid4()), reference)
        assert list(db.scalars(select(ReviewSummary))) == []
        lease = job_for(db, delivery).lease_until
        assert not complete_job(db, claimed, reference, now=lease + timedelta(seconds=1))
        assert list(db.scalars(select(ReviewSummary))) == []
        assert complete_job(db, claimed, {"report_id": str(uuid4())})
        db.commit()
        assert db.get(DeliveryWorkflow, claimed.delivery_id).state == "ai_review_partial_failure"
        assert list(db.scalars(select(ReviewFinding))) == []


@pytest.mark.parametrize("model", [ReviewReport, ReviewSummary, ReviewFinding, ReviewAnnotation])
def test_postgresql_protects_all_review_snapshots(auth_environment, model):
    with auth_environment.sessions() as db:
        schema = db.get_bind().get_execution_options()["schema_translate_map"][None]
        with pytest.raises(DBAPIError) as error:
            db.execute(text(f'UPDATE "{schema}".{model.__tablename__} SET id=id'))
        assert error.value.orig.sqlstate == "55000"
        db.rollback()


def test_outer_completed_with_missing_inner_result_is_never_completed():
    value = components()
    value["reasoning"] = {"status": "completed"}
    assert reports.normalize_components(value)[1]


def test_duplicate_handling_is_frozen_for_display_but_next_run_uses_latest(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, first, components(opinion()))
    finding = report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, finding)
    second = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, second, components(opinion()))
    assert report["opinions"][0]["annotation_revision"] == 1
    annotate(
        auth_environment,
        team,
        first,
        finding,
        handling="adopted",
        reason="重新考虑后修改",
        revision=1,
    )
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(second["id"]))
        frozen = reports.get_report_internal(db, item)["opinions"][0]
        assert frozen["handling"] == "not_adopted" and frozen["annotation_revision"] == 1
    third = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, third, components(opinion()))
    assert report["state"] == "returned" and report["opinions"][0]["duplicate_of"] is None


def test_agent_report_current_and_history_hide_foreign_duplicate_reason(auth_environment):
    team = ready_team(auth_environment, multiple_owners=True)
    first = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, first, components(opinion()))
    finding = report["opinions"][0]["finding_id"]
    annotate(auth_environment, team, first, finding, reason="FOREIGN_PRIVATE_REASON")
    second = upload_delivery(team.other_member, team, b"proof").json()
    report, _, _ = process(auth_environment, team, second, components(opinion()))
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(second["id"]))
        own = reports.get_report_internal(db, item, viewer_owner_id=UUID(team.other_member_id))
        assert own["history"] == []
        assert own["opinions"][0]["duplicate_of"] is None
        assert "FOREIGN_PRIVATE_REASON" not in str(own)
    third = upload_delivery(team.other_member, team, b"proof").json()
    process(auth_environment, team, third, components())
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(third["id"]))
        own = reports.get_report_internal(db, item, viewer_owner_id=UUID(team.other_member_id))
        assert len(own["history"]) == 1
        assert own["history"][0]["duplicate_of"] is None
        assert "FOREIGN_PRIVATE_REASON" not in str(own)


def test_old_suggestion_is_optional_and_partial_opinions_do_not_block_new_version(auth_environment):
    team = ready_team(auth_environment)
    first = submit(auth_environment, team)
    process(auth_environment, team, first, components(opinion("suggestion")))
    second = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, second)
    assert report["state"] == "pending_final_review"
    third = submit(auth_environment, team)
    process(auth_environment, team, third, components(opinion(), failure="citations"))
    # 旧任务仍待重试，领取时先排除该旧任务，不修改业务状态来伪造退回。
    with auth_environment.sessions() as db:
        old_job = job_for(db, third)
        old_job.available_at += timedelta(days=1)
        db.commit()
    fourth = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, fourth)
    assert report["state"] == "pending_final_review"


def test_source_hash_mismatch_and_unknown_component_are_rejected(auth_environment):
    team = ready_team(auth_environment)
    submit(auth_environment, team)
    claimed = claim(auth_environment, database_time(auth_environment))
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    value = components()
    value["reasoning"]["document_sha256"] = "0" * 64
    with pytest.raises(reports.ReportValidationError):
        reports.stage_report(auth_environment.sessions, review, value)
    del value["reasoning"]
    with pytest.raises(reports.ReportValidationError):
        reports.stage_report(auth_environment.sessions, review, value)
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ReviewReport))) == []


def test_history_is_limited_in_sql_and_overflow_fails_closed(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    report, _, _ = process(auth_environment, team, delivery)
    with auth_environment.sessions() as db:
        summary = db.scalar(select(ReviewSummary))
        for _ in range(501):
            db.add(ReviewFinding(summary_id=summary.id, opinion=opinion("suggestion")))
        db.commit()
    following = submit(auth_environment, team)
    with auth_environment.sessions() as db:
        item = db.get(DeliveryVersion, UUID(following["id"]))
        assert len(reports._prior_findings(db, item)) == 501
        with pytest.raises(reports.ReportValidationError):
            reports.load_review_history(db, item)


def test_coowner_annotation_uses_current_assignment_and_submitter_remains_responsible(
    auth_environment,
):
    from test_modules import publish_modules

    team = ready_team(auth_environment, multiple_owners=True)
    delivery = submit(auth_environment, team)
    result, _, _ = process(auth_environment, team, delivery, components(opinion()))
    finding = UUID(result["opinions"][0]["finding_id"])
    team.modules[0]["assignee_ids"] = [team.leader_id]
    publish_modules(team, team.modules, "移交模块负责人")
    with auth_environment.sessions() as db:
        with pytest.raises(reports.AnnotationPermissionError):
            reports.annotate_finding(
                db,
                UUID(team.project["id"]),
                UUID(delivery["id"]),
                finding,
                UUID(team.other_member_id),
                handling="adopted",
                reason="",
                expected_revision=0,
            )
        value = reports.annotate_finding(
            db,
            UUID(team.project["id"]),
            UUID(delivery["id"]),
            finding,
            UUID(team.leader_id),
            handling="adopted",
            reason="",
            expected_revision=0,
        )
        assert value["actor_id"] == team.leader_id
    assert (
        annotate(auth_environment, team, delivery, str(finding), revision=1)["actor_id"]
        == team.member_id
    )


def test_program_findings_keep_sources_and_block_only_nonconformance():
    value = components()
    value["format"] = {
        "status": "issues",
        "checker_version": "fixed-1",
        "sha256": "a" * 64,
        "findings": [
            {
                "severity": "error",
                "blocking": True,
                "location": "正文第1段",
                "message": "字体不符合",
                "spec_number": "F1",
                "requirement": "宋体",
            }
        ],
    }
    findings, incomplete = reports.normalize_components(value)
    assert not incomplete and findings[0]["severity"] == "blocking"
    assert findings[0]["source"] == "program" and findings[0]["model"] is None
    value["format"]["findings"][0]["blocking"] = False
    assert reports.normalize_components(value)[0][0]["severity"] == "suggestion"


def test_acceptance_event_failure_rolls_back_summary_findings_and_state(
    auth_environment, monkeypatch
):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    claimed = claim(auth_environment, database_time(auth_environment))
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    reference = reports.stage_report(auth_environment.sessions, review, components(opinion()))
    original = reports.record_event

    def fail(db, **arguments):
        original(db, **arguments)
        raise RuntimeError("fixed late write failure")

    with monkeypatch.context() as patch:
        patch.setattr(reports, "record_event", fail)
        with auth_environment.sessions() as db:
            with pytest.raises(RuntimeError):
                complete_job(db, claimed, reference)
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(ReviewSummary))) == []
        assert list(db.scalars(select(ReviewFinding))) == []
        assert job_for(db, delivery).status == "running"
        assert db.get(DeliveryWorkflow, claimed.delivery_id).state == "ai_reviewing"
        assert complete_job(db, claimed, reference)
        db.commit()


def test_partial_report_uses_configured_retry_delay(auth_environment):
    team = ready_team(auth_environment)
    delivery = submit(auth_environment, team)
    current = database_time(auth_environment)
    claimed = claim(auth_environment, current)
    review = DeliveryReviewInput(
        claimed.job_id,
        claimed.delivery_id,
        claimed.project_id,
        claimed.attempt,
        claimed.workflow_revision,
    )
    reference = reports.stage_report(
        auth_environment.sessions, review, components(failure="reasoning")
    )
    with auth_environment.sessions() as db:
        assert complete_job(db, claimed, reference, now=current, retry_delay_seconds=19)
        db.commit()
        assert job_for(db, delivery).available_at == current + timedelta(seconds=19)
