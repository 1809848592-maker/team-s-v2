"""人类显式重试失败审核；权限由发起人在 Q-M3-14-01 中确定。"""

from uuid import UUID

from sqlalchemy import select

from app.events import record_event
from app.models import DeliveryVersion, DeliveryWorkflow, Project, ProjectMember, ReviewJob
from app.schemas.plans import PlanContent
from app.schemas.review_jobs import ReviewProgress
from app.services import projects, review_jobs
from app.services.plans import get_latest_plan


class ReviewRetryPermissionError(Exception):
    pass


class ReviewRetryNotFoundError(Exception):
    pass


def _member(db, project_id, user_id):
    # 查询列值，避免认证依赖已缓存的成员对象遮蔽锁等待期间的权限变化。
    return (
        db.scalar(
            select(ProjectMember.user_id).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
        is not None
    )


def _responsible(db, item, user_id):
    if user_id == item.submitted_by:
        return True
    plan = get_latest_plan(db, item.project_id)
    if plan is None:
        return False
    module = next(
        (
            entry
            for entry in PlanContent.model_validate(plan.content).modules
            if entry.id == item.module_id
        ),
        None,
    )
    return module is not None and user_id in module.assignee_ids


def can_retry_review(db, item: DeliveryVersion, user_id: UUID) -> bool:
    if not _member(db, item.project_id, user_id) or not _responsible(db, item, user_id):
        return False
    workflow = db.get(DeliveryWorkflow, item.id, populate_existing=True)
    job = db.scalar(
        select(ReviewJob)
        .where(ReviewJob.delivery_id == item.id)
        .execution_options(populate_existing=True)
    )
    return bool(
        workflow is not None
        and job is not None
        and workflow.state == "ai_review_partial_failure"
        and job.status == "failed"
        and workflow.revision == job.workflow_revision
    )


def request_retry(db, project_id, delivery_id, user_id, *, expected_revision):
    try:
        if not _member(db, project_id, user_id):
            raise projects.ProjectNotFoundError
        if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
            raise projects.ProjectNotFoundError
        if not _member(db, project_id, user_id):
            raise projects.ProjectNotFoundError
        item = db.scalar(
            select(DeliveryVersion).where(
                DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
            )
        )
        if item is None:
            raise ReviewRetryNotFoundError
        if not _responsible(db, item, user_id):
            raise ReviewRetryPermissionError
        workflow = db.scalar(
            select(DeliveryWorkflow)
            .where(DeliveryWorkflow.delivery_id == item.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            type(expected_revision) is not int
            or workflow is None
            or workflow.revision != expected_revision
        ):
            raise review_jobs.ReviewJobConflictError("审核状态已变化，请刷新后核对")
        job = review_jobs.retry_job(db, item.id)
        record_event(
            db,
            project_id=project_id,
            actor_id=user_id,
            event_type="review_retry_requested",
            object_type="delivery_version",
            object_id=item.id,
            task_id=item.module_id,
            plan_version_id=item.plan_version_id,
            spec_version_id=item.spec_version_id,
            details={
                "job_id": str(job.id),
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "workflow_revision": workflow.revision,
            },
        )
        result = ReviewProgress.model_validate(job)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
