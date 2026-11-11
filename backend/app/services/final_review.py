"""项目负责人终审：锁内核验身份和完整处理快照，原子保存人的决定。"""

import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import or_, select

from app.events import record_event
from app.models import (
    DeliveryVersion,
    DeliveryWorkflow,
    FinalReviewDecision,
    Project,
    ProjectMember,
)
from app.models.review_report import ReviewFinding, ReviewSummary
from app.schemas.final_reviews import FinalReviewCreate
from app.services import review_reports
from app.services.delivery_states import transition_review
from app.services.projects import ProjectNotFoundError

MAX_FINAL_FINDINGS = 5000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


class FinalReviewNotFoundError(Exception):
    pass


class FinalReviewPermissionError(Exception):
    pass


class FinalReviewConflictError(Exception):
    pass


class FinalReviewValidationError(ValueError):
    pass


def _role(db, project_id, user_id):
    role = db.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
        )
    )
    if role is None:
        raise ProjectNotFoundError
    return role


def _leaders(db, project_id):
    return list(
        db.scalars(
            select(ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id, ProjectMember.role == "leader")
            .order_by(ProjectMember.user_id)
        )
    )


def _source(db, project_id, delivery_id, user_id, *, write=False):
    _role(db, project_id, user_id)
    if (
        db.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update(read=not write)
        )
        is None
    ):
        raise ProjectNotFoundError
    _role(db, project_id, user_id)
    item = db.scalar(
        select(DeliveryVersion).where(
            DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
        )
    )
    if item is None:
        raise FinalReviewNotFoundError
    workflow = db.scalar(
        select(DeliveryWorkflow)
        .where(DeliveryWorkflow.delivery_id == item.id)
        .with_for_update(read=not write)
        .execution_options(populate_existing=True)
    )
    if workflow is None:
        raise FinalReviewConflictError
    return item, workflow, _leaders(db, project_id)


def _entries(db, item, summary):
    # 终审独立读取全部相关意见，不能复用报告页面的500条历史展示上限。
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
    rows = db.execute(
        select(ReviewFinding, DeliveryVersion.id, DeliveryVersion.version)
        .select_from(ReviewFinding)
        .join(ReviewSummary, ReviewSummary.id == ReviewFinding.summary_id)
        .join(DeliveryVersion, DeliveryVersion.id == ReviewSummary.delivery_id)
        .where(
            or_(
                ReviewSummary.id == summary.id,
                (ReviewSummary.id.in_(latest)) & (ReviewSummary.status == "completed"),
            )
        )
        .order_by(DeliveryVersion.version.desc(), ReviewFinding.id)
        .limit(MAX_FINAL_FINDINGS + 1)
    ).all()
    if len(rows) > MAX_FINAL_FINDINGS:
        raise FinalReviewValidationError("终审意见超过处理上限，不能省略意见后通过。")
    return [
        {
            **review_reports._finding_info(db, finding),
            "delivery_id": str(delivery_id),
            "delivery_version": version,
        }
        for finding, delivery_id, version in rows
    ]


def _rejected_groups(entries):
    grouped = {}
    for entry in entries:
        if entry["handling"] != "not_adopted":
            continue
        key = (
            entry["duplicate_of"] or entry["finding_id"],
            entry["annotation_revision"],
            entry["actor_id"],
            entry["reason"],
        )
        if key not in grouped:
            grouped[key] = {**entry, "related_finding_ids": []}
        grouped[key]["related_finding_ids"].append(entry["finding_id"])
    return list(grouped.values())


def _snapshot(db, item, workflow, leaders):
    summary = review_reports._latest_summary(db, item.id)
    if summary is None or summary.status not in {"completed", "bypassed"}:
        raise FinalReviewConflictError
    entries = _entries(db, item, summary)
    rejected = _rejected_groups(entries)
    snapshot = {
        "project_id": str(item.project_id),
        "delivery_id": str(item.id),
        "module_id": str(item.module_id),
        "delivery_version": item.version,
        "submitted_by": str(item.submitted_by),
        "plan_version_id": str(item.plan_version_id),
        "spec_version_id": str(item.spec_version_id),
        "workflow_revision": workflow.revision,
        "state": workflow.state,
        "summary_id": str(summary.id),
        "report_id": str(summary.report_id) if summary.report_id else None,
        "leader_ids": [str(identity) for identity in leaders],
        "nonconverged": summary.nonconverged,
        "round": summary.round,
        "opinions": [entry for entry in entries if entry["delivery_id"] == str(item.id)],
        "history": [entry for entry in entries if entry["delivery_id"] != str(item.id)],
        "not_adopted": rejected,
        "specification_exceptions": [
            entry for entry in rejected if entry["opinion"]["category"] == "specification"
        ],
    }
    encoded = json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    if len(encoded.encode()) > MAX_SNAPSHOT_BYTES:
        raise FinalReviewValidationError("终审依据超过处理上限，不能省略依据后通过。")
    return summary, snapshot, hashlib.sha256(encoded.encode()).hexdigest()


def _decision_info(item):
    return {
        "id": str(item.id),
        "actor_id": str(item.actor_id),
        "outcome": item.outcome,
        "requested_decision": item.requested_decision,
        "self_review": item.self_review,
        "comment": item.comment,
        "spec_rulings": item.spec_rulings,
        "context_hash": item.context_hash,
        "workflow_revision": item.workflow_revision,
        "created_at": item.created_at.isoformat(),
    }


def decision_for_report(db, item, *, visible_finding_ids=None):
    decision = db.scalar(
        select(FinalReviewDecision).where(FinalReviewDecision.delivery_id == item.id)
    )
    if decision is None:
        return None
    result = _decision_info(decision)
    if visible_finding_ids is not None:
        result["spec_rulings"] = [
            ruling
            for ruling in result["spec_rulings"]
            if ruling["finding_id"] in visible_finding_ids
        ]
    return result


def get_final_review(db, project_id, delivery_id, user_id):
    with db.no_autoflush:
        item, workflow, leaders = _source(db, project_id, delivery_id, user_id)
        decision = db.scalar(
            select(FinalReviewDecision).where(FinalReviewDecision.delivery_id == item.id)
        )
        if decision is not None:
            snapshot, context_hash = decision.snapshot, decision.context_hash
        else:
            _, snapshot, context_hash = _snapshot(db, item, workflow, leaders)
        self_review = user_id == item.submitted_by
        return {
            **snapshot,
            "state": workflow.state,
            "workflow_revision": workflow.revision,
            "context_hash": context_hash,
            "can_decide": workflow.state == "pending_final_review"
            and user_id in leaders
            and (not self_review or len(leaders) == 1),
            "self_review": self_review and user_id in leaders and len(leaders) == 1,
            "report": review_reports.get_report_internal(db, item),
            "decision": _decision_info(decision) if decision else None,
        }


def decide_final_review(db, project_id, delivery_id, user_id, body):
    try:
        try:
            body = FinalReviewCreate.model_validate(body)
        except (ValidationError, TypeError, ValueError):
            raise FinalReviewValidationError from None
        with db.no_autoflush:
            item, workflow, leaders = _source(db, project_id, delivery_id, user_id, write=True)
            if user_id not in leaders:
                raise FinalReviewPermissionError
            self_review = user_id == item.submitted_by
            if self_review and len(leaders) != 1:
                raise FinalReviewPermissionError
            if (
                workflow.state != "pending_final_review"
                or workflow.revision != body.expected_revision
            ):
                raise FinalReviewConflictError
            if (
                db.scalar(
                    select(FinalReviewDecision.id).where(FinalReviewDecision.delivery_id == item.id)
                )
                is not None
            ):
                raise FinalReviewConflictError
            summary, snapshot, context_hash = _snapshot(db, item, workflow, leaders)
            if body.context_hash != context_hash:
                raise FinalReviewConflictError
            expected = {entry["finding_id"] for entry in snapshot["specification_exceptions"]}
            supplied = {str(ruling.finding_id) for ruling in body.spec_rulings}
            if not supplied <= expected or (body.decision == "approve" and supplied != expected):
                raise FinalReviewValidationError("请逐条裁定本次列出的规范不适用项。")
            reject = any(ruling.decision == "reject" for ruling in body.spec_rulings)
            outcome = "final_returned" if body.decision == "return" or reject else "adopted"
            rulings = [ruling.model_dump(mode="json") for ruling in body.spec_rulings]
            identity = uuid4()
            transition_review(
                db,
                project_id,
                item.id,
                outcome,
                expected_state=workflow.state,
                expected_revision=workflow.revision,
                actor_id=user_id,
                execution_context={
                    "final_review_id": str(identity),
                    "self_review": self_review,
                    "context_hash": context_hash,
                },
            )
            event = record_event(
                db,
                project_id=project_id,
                actor_id=user_id,
                event_type="delivery_final_reviewed",
                object_type="delivery_version",
                object_id=item.id,
                task_id=item.module_id,
                plan_version_id=item.plan_version_id,
                spec_version_id=item.spec_version_id,
                details={
                    "final_review_id": str(identity),
                    "summary_id": str(summary.id),
                    "outcome": outcome,
                    "self_review": self_review,
                    "context_hash": context_hash,
                    "spec_ruling_count": len(rulings),
                },
            )
            decision = FinalReviewDecision(
                id=identity,
                delivery_id=item.id,
                summary_id=summary.id,
                actor_id=user_id,
                workflow_revision=body.expected_revision,
                requested_decision=body.decision,
                outcome=outcome,
                self_review=self_review,
                context_hash=context_hash,
                comment=body.comment.strip(),
                spec_rulings=rulings,
                snapshot=snapshot,
                event_id=event.id,
            )
            db.add(decision)
            db.flush()
            result = _decision_info(decision)
            db.commit()
            return result
    except Exception:
        db.rollback()
        raise
