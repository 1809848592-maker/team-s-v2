"""完整报告持久化、有效租约下汇总，以及人类的追加式意见标注。"""

import hashlib
import json
import unicodedata
from collections import Counter
from uuid import UUID

from sqlalchemy import func, select

from app.events import record_event
from app.models import DeliveryVersion, DeliveryWorkflow, Project, ReviewJob
from app.models.review_report import ReviewAnnotation, ReviewFinding, ReviewReport, ReviewSummary
from app.review.reviewers.schemas import HistoricalOpinion, OpinionDraft, RecordedOpinion
from app.schemas.plans import PlanContent
from app.schemas.review_jobs import ReviewProgress
from app.services.delivery_states import transition_review
from app.services.plans import get_latest_plan
from app.services.projects import require_member

SIMILARITY_THRESHOLD = 0.88
MAX_REPORT_BYTES = 32 * 1024 * 1024


class ReportNotFoundError(Exception):
    pass


class AnnotationPermissionError(Exception):
    pass


class AnnotationConflictError(Exception):
    pass


class AnnotationValidationError(ValueError):
    pass


class ReportValidationError(ValueError):
    pass


def _latest_summary(db, delivery_id):
    return db.scalar(
        select(ReviewSummary)
        .where(ReviewSummary.delivery_id == delivery_id)
        .order_by(ReviewSummary.created_at.desc(), ReviewSummary.id.desc())
        .limit(1)
    )


def _prior_findings(db, item, *, viewer_owner_id=None):
    # 每个旧版本只取最后一次被有效领取接受的报告，不把重试意见重复计入历史。
    latest = (
        select(ReviewSummary.id)
        .join(DeliveryVersion)
        .where(
            DeliveryVersion.project_id == item.project_id,
            DeliveryVersion.module_id == item.module_id,
            DeliveryVersion.version < item.version,
        )
        .distinct(ReviewSummary.delivery_id)
        .order_by(
            ReviewSummary.delivery_id, ReviewSummary.created_at.desc(), ReviewSummary.id.desc()
        )
    )
    if viewer_owner_id is not None:
        latest = latest.where(DeliveryVersion.submitted_by == viewer_owner_id)
    return list(
        db.scalars(
            select(ReviewFinding)
            .join(ReviewSummary)
            .where(ReviewFinding.summary_id.in_(latest), ReviewSummary.status == "completed")
            .order_by(ReviewFinding.created_at, ReviewFinding.id)
            .limit(501)
        )
    )


def _annotation(db, identity):
    return db.scalar(
        select(ReviewAnnotation)
        .where(ReviewAnnotation.finding_id == identity)
        .order_by(ReviewAnnotation.revision.desc())
        .limit(1)
    )


def _handled(db, finding, *, live=False):
    if finding.duplicate_of and finding.duplicate_handling and not live:
        return finding.duplicate_handling
    target = finding.duplicate_of or finding.id
    annotation = _annotation(db, target)
    return {
        "handling": annotation.handling if annotation else "unhandled",
        "reason": annotation.reason if annotation else "",
        "actor_id": str(annotation.actor_id) if annotation else None,
        "annotation_revision": annotation.revision if annotation else 0,
        "annotated_at": annotation.created_at.isoformat() if annotation else None,
    }


def load_review_history(db, item):
    findings = _prior_findings(db, item)
    if len(findings) > 500:
        raise ReportValidationError("历史意见超出处理上限，审核未完成。")
    result = []
    for finding in findings:
        handling = _handled(db, finding, live=True)
        result.append(
            HistoricalOpinion.model_validate(
                {
                    "finding_id": finding.id,
                    "opinion": OpinionDraft.model_validate(
                        {
                            key: finding.opinion[key]
                            for key in OpinionDraft.model_fields
                            if key in finding.opinion
                        }
                    ),
                    "handling": handling["handling"],
                    "reason": handling["reason"],
                    "actor_id": UUID(handling["actor_id"]) if handling["actor_id"] else None,
                }
            )
        )
    return result


def _normal(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def _match_key(opinion):
    location = opinion["location"]
    coordinates = {
        key: value for key, value in location.items() if key != "description" and value is not None
    }
    if not coordinates:
        coordinates = {"description": location["description"]}
    return (
        opinion["category"],
        json.dumps(coordinates, sort_keys=True),
        opinion["basis"].get("spec_number"),
        opinion["basis"].get("excerpt") if opinion["category"] == "citation" else None,
    )


def same_problem(current, previous):
    if _match_key(current) != _match_key(previous):
        return False
    left, right = _normal(current["problem"]), _normal(previous["problem"])
    if not left or not right:
        return False
    if left == right:
        return True
    # 字符二元组多重集合Dice系数，线性构造，避免重复文本的平方级序列比对。
    a = Counter(left[i : i + 2] for i in range(max(1, len(left) - 1)))
    b = Counter(right[i : i + 2] for i in range(max(1, len(right) - 1)))
    return 2 * sum((a & b).values()) / (sum(a.values()) + sum(b.values())) >= SIMILARITY_THRESHOLD


def _model_findings(result):
    if not isinstance(result, dict) or result.get("status") != "completed":
        return []
    return [
        RecordedOpinion.model_validate_json(json.dumps(value)).model_dump(mode="json")
        for value in result["findings"]
    ]


def normalize_components(components):
    if not isinstance(components, dict) or set(components) != {
        "format",
        "specification",
        "citations",
        "reasoning",
    }:
        raise ReportValidationError("审核组件不完整。")
    findings, incomplete = [], False
    for name, report in components.items():
        if not isinstance(report, dict):
            raise ReportValidationError("审核组件结构无效。")
        status = report.get("status")
        allowed = {
            "format": {"passed", "partial", "unsupported", "not_applicable", "issues"},
            "specification": {"completed", "no_ai_items"},
            "citations": {"completed"},
            "reasoning": {"completed"},
        }[name]
        incomplete |= status not in allowed
        required = {
            "specification": ("review",) if status == "completed" else (),
            "citations": ("extraction", "alignment") if status == "completed" else (),
            "reasoning": ("logic", "linkage") if status == "completed" else (),
        }.get(name, ())
        incomplete |= any(
            not isinstance(report.get(key), dict) or report[key].get("status") != "completed"
            for key in required
        )
        if name == "format" and status in allowed:
            for raw in report.get("findings", []):
                opinion = OpinionDraft.model_validate(
                    {
                        "category": "specification",
                        "severity": "blocking" if raw["blocking"] else "suggestion",
                        "location": {"description": raw["location"]},
                        "problem": raw["message"],
                        "basis": {"spec_number": raw["spec_number"]},
                        "suggestion": "请对照绑定规范修改，并在新版本中核对。",
                    }
                ).model_dump(mode="json")
                findings.append(
                    {
                        **opinion,
                        "source": "program",
                        "reviewer": "format_checker",
                        "instruction_version": report["checker_version"],
                        "model": None,
                        "input_hash": report["sha256"],
                        "instruction_hash": hashlib.sha256(raw["requirement"].encode()).hexdigest(),
                        "call_id": None,
                    }
                )
        elif name == "specification":
            findings.extend(_model_findings(report.get("review")))
        elif name == "citations":
            for raw in report.get("database_findings", []) if status == "completed" else []:
                OpinionDraft.model_validate(
                    {key: raw[key] for key in OpinionDraft.model_fields if key in raw}
                )
                findings.append(raw)
            findings.extend(_model_findings(report.get("alignment")))
        elif name == "reasoning":
            findings.extend(_model_findings(report.get("logic")))
            findings.extend(_model_findings(report.get("linkage")))
    if len(findings) > 500:
        raise ReportValidationError("本轮意见超出处理上限。")
    return findings, incomplete


def stage_report(sessions, review, components):
    try:
        encoded = json.dumps(components, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_REPORT_BYTES:
            raise ValueError
        normalize_components(components)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ReportValidationError("审核结果结构无效或超出处理上限。") from None
    with sessions() as db:
        # 与领取/正式提交一致的项目→工作流→任务锁序；不持锁执行模型。
        db.scalar(select(Project.id).where(Project.id == review.project_id).with_for_update())
        item = db.get(DeliveryVersion, review.delivery_id)
        workflow = db.scalar(
            select(DeliveryWorkflow)
            .where(DeliveryWorkflow.delivery_id == review.delivery_id)
            .with_for_update()
        )
        job = db.scalar(select(ReviewJob).where(ReviewJob.id == review.job_id).with_for_update())
        current = db.scalar(select(func.clock_timestamp()))
        if (
            item is None
            or item.project_id != review.project_id
            or workflow is None
            or job is None
            or job.delivery_id != item.id
            or job.status != "running"
            or job.attempt != review.attempt
            or workflow.state != "ai_reviewing"
            or workflow.revision != review.workflow_revision
            or job.workflow_revision != review.workflow_revision
            or job.lease_until <= current
        ):
            raise ReportValidationError("审核领取已失效。")
        for name, component in components.items():
            for key, expected in (
                ("delivery_id", item.id),
                ("job_id", job.id),
                ("project_id", item.project_id),
                ("spec_version_id", item.spec_version_id),
                ("plan_version_id", item.plan_version_id),
            ):
                if key in component and component[key] != str(expected):
                    raise ReportValidationError("审核来源不匹配。")
            digest = component.get("sha256" if name == "format" else "document_sha256")
            if digest is not None and digest != item.sha256:
                raise ReportValidationError("审核原文件来源不匹配。")
        report = ReviewReport(
            delivery_id=item.id,
            job_id=job.id,
            attempt=review.attempt,
            workflow_revision=review.workflow_revision,
            components=json.loads(encoded),
        )
        db.add(report)
        db.commit()
        return {"report_id": str(report.id)}


def return_count(db, item):
    return db.scalar(
        select(func.count())
        .select_from(DeliveryVersion)
        .join(DeliveryWorkflow)
        .where(
            DeliveryVersion.project_id == item.project_id,
            DeliveryVersion.module_id == item.module_id,
            DeliveryVersion.version < item.version,
            DeliveryWorkflow.state == "returned",
        )
    )


def accept_report(db, item, workflow, job, claim, result):
    """调用方已核验有效租约，事务由complete_job提交。"""
    try:
        if set(result) != {"report_id"}:
            raise ValueError
        report = db.get(ReviewReport, UUID(result["report_id"]))
        if (
            report is None
            or report.delivery_id != item.id
            or report.job_id != job.id
            or report.attempt != claim.attempt
            or report.workflow_revision != claim.workflow_revision
            or db.scalar(select(ReviewSummary.id).where(ReviewSummary.report_id == report.id))
            is not None
        ):
            raise ValueError
        findings, incomplete = normalize_components(report.components)
        history = _prior_findings(db, item)
        if len(history) > 500:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ReportValidationError("审核报告引用无效。") from None
    summary = ReviewSummary(
        delivery_id=item.id,
        report_id=report.id,
        status="incomplete" if incomplete else "completed",
        round=return_count(db, item) + 1,
    )
    db.add(summary)
    db.flush()
    rejected = [(old, _handled(db, old, live=True)) for old in history]
    rejected = [
        (old, handling) for old, handling in rejected if handling["handling"] == "not_adopted"
    ]
    blocking = any(
        old.opinion["severity"] != "suggestion"
        and _handled(db, old, live=True)["handling"] == "unhandled"
        for old in history
    )
    comparisons = 0
    for opinion in findings:
        candidates = [
            old
            for old, handling in reversed(rejected)
            if _match_key(opinion) == _match_key(old.opinion)
        ]
        comparisons += len(candidates)
        if comparisons > 2000:
            raise ReportValidationError("相似意见比较超过处理上限。")
        match = next(
            (old for old in candidates if same_problem(opinion, old.opinion)),
            None,
        )
        finding = ReviewFinding(
            summary_id=summary.id,
            opinion=opinion,
            duplicate_of=(match.duplicate_of or match.id) if match else None,
            duplicate_handling=_handled(db, match, live=True) if match else None,
        )
        db.add(finding)
        blocking |= match is None and opinion["severity"] in {"blocking", "important"}
    db.flush()
    record_event(
        db,
        project_id=item.project_id,
        actor_type="agent",
        actor_id="platform-review-worker",
        event_type="review_report_accepted",
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={
            "report_id": str(report.id),
            "summary_id": str(summary.id),
            "attempt": job.attempt,
            "status": summary.status,
            "finding_count": len(findings),
            "similarity_threshold": SIMILARITY_THRESHOLD,
        },
    )
    return summary, "returned" if blocking else "pending_final_review"


def maybe_bypass_review(db, item, workflow, *, limit):
    count = return_count(db, item)
    if count < limit:
        return False
    summary = ReviewSummary(
        delivery_id=item.id, status="bypassed", round=count + 1, nonconverged=True
    )
    db.add(summary)
    db.flush()
    transition_review(
        db,
        item.project_id,
        item.id,
        "pending_final_review",
        expected_state=workflow.state,
        expected_revision=workflow.revision,
        actor_id="platform-review-worker",
        actor_type="agent",
        execution_context={
            "execution_origin": "platform_worker",
            "nonconverged": True,
            "round": count + 1,
        },
    )
    record_event(
        db,
        project_id=item.project_id,
        actor_type="agent",
        actor_id="platform-review-worker",
        event_type="review_loop_limit_reached",
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={"summary_id": str(summary.id), "round": count + 1, "return_limit": limit},
    )
    return True


def _finding_info(db, finding):
    return {
        "finding_id": str(finding.id),
        "opinion": finding.opinion,
        "duplicate_of": str(finding.duplicate_of) if finding.duplicate_of else None,
        **_handled(db, finding),
    }


def get_report_internal(db, item, *, viewer_owner_id=None):
    summary = _latest_summary(db, item.id)
    if summary is None:
        return {
            "coverage": "not_covered",
            "opinions": [],
            "note": "审核尚未生成报告，未覆盖；没有审核结论。",
        }
    report = db.get(ReviewReport, summary.report_id) if summary.report_id else None
    opinions = list(
        db.scalars(
            select(ReviewFinding)
            .where(ReviewFinding.summary_id == summary.id)
            .order_by(ReviewFinding.created_at, ReviewFinding.id)
        )
    )
    history = _prior_findings(db, item, viewer_owner_id=viewer_owner_id)
    visible_ids = None
    if viewer_owner_id is not None:
        linked_ids = {
            finding.duplicate_of for finding in [*opinions, *history] if finding.duplicate_of
        }
        visible_ids = set(
            db.scalars(
                select(ReviewFinding.id)
                .join(ReviewSummary)
                .join(DeliveryVersion)
                .where(
                    DeliveryVersion.project_id == item.project_id,
                    DeliveryVersion.submitted_by == viewer_owner_id,
                    DeliveryVersion.module_id == item.module_id,
                    DeliveryVersion.version <= item.version,
                    ReviewFinding.id.in_(linked_ids | {finding.id for finding in history}),
                )
            )
        )
        history = [finding for finding in history if finding.id in visible_ids]

    def render(finding):
        value = _finding_info(db, finding)
        if (
            finding.duplicate_of
            and visible_ids is not None
            and finding.duplicate_of not in visible_ids
        ):
            value.update(
                duplicate_of=None,
                handling="unhandled",
                reason="",
                actor_id=None,
                annotation_revision=0,
                annotated_at=None,
            )
        return value

    from app.services.final_review import decision_for_report

    allowed = None
    if viewer_owner_id is not None:
        # 终审意见供本人返工；他人历史规范项裁定仍不随本人报告外泄。
        allowed = {str(finding.id) for finding in opinions}
        allowed.update(str(identity) for identity in visible_ids or ())
    final_decision = decision_for_report(db, item, visible_finding_ids=allowed)

    return {
        "coverage": summary.status,
        "report_id": str(report.id) if report else None,
        "state": item.review_status,
        "round": summary.round,
        "nonconverged": summary.nonconverged,
        "note": f"AI 审核未收敛，第 {summary.round} 轮，直接进入终审。"
        if summary.nonconverged
        else "AI 生成的审核意见，供参考；未覆盖范围仍需人工确认。",
        "opinions": [render(finding) for finding in opinions],
        "components": report.components if report else {},
        "history": [render(finding) for finding in history[:500]],
        "history_truncated": len(history) > 500,
        "final_review": final_decision,
    }


def get_review_report(db, project_id, delivery_id, user_id):
    from app.services.review_retry import can_retry_review

    require_member(db, project_id, user_id)
    item = db.scalar(
        select(DeliveryVersion).where(
            DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
        )
    )
    if item is None:
        raise ReportNotFoundError
    result = get_report_internal(db, item)
    workflow = item.review_workflow
    plan = get_latest_plan(db, project_id)
    module = (
        next(
            (
                entry
                for entry in PlanContent.model_validate(plan.content).modules
                if entry.id == item.module_id
            ),
            None,
        )
        if plan
        else None
    )
    can_handle = user_id == item.submitted_by or (
        module is not None and user_id in module.assignee_ids
    )
    # These capabilities describe this human view only. Agent/internal report contracts
    # and their narrower history filtering must remain unchanged.
    result.update(
        can_annotate=bool(
            can_handle
            and workflow is not None
            and workflow.state in {"returned", "pending_final_review"}
            and any(finding["duplicate_of"] is None for finding in result["opinions"])
        ),
        can_retry=can_retry_review(db, item, user_id),
        workflow_revision=workflow.revision if workflow is not None else None,
        review_progress=(
            ReviewProgress.model_validate(item.review_progress).model_dump(mode="json")
            if item.review_progress is not None
            else None
        ),
    )
    visible = [*result["opinions"], *result.get("history", [])]
    if visible:
        source_rows = db.execute(
            select(ReviewFinding.id, DeliveryVersion.id, DeliveryVersion.version)
            .join(ReviewSummary, ReviewSummary.id == ReviewFinding.summary_id)
            .join(DeliveryVersion, DeliveryVersion.id == ReviewSummary.delivery_id)
            .where(
                ReviewFinding.id.in_([UUID(finding["finding_id"]) for finding in visible]),
                DeliveryVersion.project_id == item.project_id,
                DeliveryVersion.module_id == item.module_id,
                DeliveryVersion.version <= item.version,
            )
        ).all()
        sources = {
            str(finding_id): {"delivery_id": str(source_id), "delivery_version": version}
            for finding_id, source_id, version in source_rows
        }
        for finding in visible:
            finding.update(sources[finding["finding_id"]])
    return result


def annotate_finding(
    db, project_id, delivery_id, finding_id, user_id, *, handling, reason, expected_revision
):
    try:
        require_member(db, project_id, user_id)
        if (
            handling not in {"adopted", "not_adopted"}
            or not isinstance(reason, str)
            or len(reason) > 4000
            or "\x00" in reason
            or (handling == "not_adopted" and not reason.strip())
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise AnnotationValidationError
        db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
        require_member(db, project_id, user_id)
        item = db.scalar(
            select(DeliveryVersion).where(
                DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
            )
        )
        if item is None:
            raise ReportNotFoundError
        plan = get_latest_plan(db, project_id)
        module = (
            next(
                (
                    entry
                    for entry in PlanContent.model_validate(plan.content).modules
                    if entry.id == item.module_id
                ),
                None,
            )
            if plan
            else None
        )
        if user_id != item.submitted_by and (module is None or user_id not in module.assignee_ids):
            raise AnnotationPermissionError
        workflow = db.scalar(
            select(DeliveryWorkflow)
            .where(DeliveryWorkflow.delivery_id == item.id)
            .with_for_update()
        )
        if workflow is None or workflow.state not in {
            "returned",
            "pending_final_review",
        }:
            raise AnnotationConflictError
        finding = db.get(ReviewFinding, finding_id)
        summary = db.get(ReviewSummary, finding.summary_id) if finding else None
        if summary is None or summary.delivery_id != item.id:
            raise ReportNotFoundError
        if _latest_summary(db, item.id).id != summary.id:
            raise AnnotationConflictError
        if finding.duplicate_of:
            raise AnnotationConflictError
        previous = _annotation(db, finding.id)
        if expected_revision != (previous.revision if previous else 0):
            raise AnnotationConflictError
        annotation = ReviewAnnotation(
            finding_id=finding.id,
            revision=expected_revision + 1,
            handling=handling,
            reason=reason.strip(),
            actor_id=user_id,
        )
        db.add(annotation)
        db.flush()
        record_event(
            db,
            project_id=item.project_id,
            actor_id=user_id,
            event_type="review_finding_annotated",
            object_type="review_finding",
            object_id=finding.id,
            task_id=item.module_id,
            plan_version_id=item.plan_version_id,
            spec_version_id=item.spec_version_id,
            details={
                "delivery_id": str(item.id),
                "annotation_id": str(annotation.id),
                "revision": annotation.revision,
                "handling": handling,
            },
        )
        result = _finding_info(db, finding)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
