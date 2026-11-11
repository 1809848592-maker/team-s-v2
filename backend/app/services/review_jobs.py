"""持久队列、有限重试和租约隔离。执行器必须在事务外运行。

所有写操作按 project → workflow → job 加锁；本层不提交事务。
外部调用可能被重做，只有仍持有效领取凭证的执行器能提交数据库结果。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.delivery import DeliveryVersion
from app.models.project import Project
from app.models.review_job import ReviewJob
from app.models.review_workflow import DeliveryWorkflow
from app.services.delivery_states import transition_review

WORKER_ACTOR = "platform-review-worker"
ERROR_CODES = frozenset(
    {
        "pipeline_not_ready",
        "execution_error",
        "timeout",
        "lease_expired",
        "stale_workflow",
        "quota_exceeded",
    }
)


class ReviewJobConflictError(ValueError):
    pass


@dataclass(frozen=True)
class JobClaim:
    job_id: UUID
    delivery_id: UUID
    project_id: UUID
    claim_token: UUID
    attempt: int
    workflow_revision: int


def _atomic(operation):
    @wraps(operation)
    def wrapped(db: Session, *args, **kwargs):
        try:
            return operation(db, *args, **kwargs)
        except Exception:
            db.rollback()
            raise

    return wrapped


def _now(db: Session, override: datetime | None) -> datetime:
    value = override if override is not None else db.scalar(select(func.clock_timestamp()))
    if value.tzinfo is None:
        raise ValueError("队列时间必须带时区")
    return value.astimezone(UTC)


def _context(job: ReviewJob, item: DeliveryVersion) -> dict:
    return {
        "execution_origin": "platform_worker",
        "job_id": str(job.id),
        "attempt": job.attempt,
        "initiated_by": str(item.submitted_by),
    }


def _event(db: Session, job: ReviewJob, item: DeliveryVersion, event_type: str) -> None:
    record_event(
        db,
        project_id=item.project_id,
        actor_type="agent",
        actor_id=WORKER_ACTOR,
        event_type=event_type,
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={
            **_context(job, item),
            "status": job.status,
            "last_error": job.last_error,
            "max_attempts": job.max_attempts,
        },
    )


def _lock(db: Session, job_id: UUID, *, skip_locked: bool = False):
    item = db.scalar(select(DeliveryVersion).join(ReviewJob).where(ReviewJob.id == job_id))
    if item is None:
        return None
    project = db.scalar(
        select(Project.id)
        .where(Project.id == item.project_id)
        .with_for_update(skip_locked=skip_locked)
    )
    if project is None:
        return None
    workflow = db.scalar(
        select(DeliveryWorkflow)
        .where(DeliveryWorkflow.delivery_id == item.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = db.scalar(
        select(ReviewJob)
        .where(ReviewJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return item, workflow, job


def _refresh(db: Session, item: DeliveryVersion) -> None:
    db.flush()
    db.expire(item, ["review_job", "review_workflow"])


@_atomic
def enqueue_review(db: Session, item: DeliveryVersion, workflow: DeliveryWorkflow) -> ReviewJob:
    """正式提交已持项目锁；与交付、工作流、确认关系在同一事务创建。"""
    if db.scalar(select(ReviewJob.id).where(ReviewJob.delivery_id == item.id)) is not None:
        raise ReviewJobConflictError("该正式版本已创建审核任务")
    current = _now(db, None)
    job = ReviewJob(
        delivery_id=item.id,
        workflow_revision=workflow.revision,
        available_at=current,
        updated_at=current,
    )
    db.add(job)
    db.flush()
    record_event(
        db,
        project_id=item.project_id,
        actor_id=item.submitted_by,
        event_type="review_job_queued",
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={"job_id": str(job.id), "status": "queued", "attempt": 0},
    )
    _refresh(db, item)
    return job


def _change_workflow(db, item, workflow, job, target):
    changed = transition_review(
        db,
        item.project_id,
        item.id,
        target,
        expected_state=workflow.state,
        expected_revision=workflow.revision,
        actor_id=WORKER_ACTOR,
        actor_type="agent",
        execution_context=_context(job, item),
    )
    job.workflow_revision = changed.revision


def _fail_locked(db, item, workflow, job, code, retryable, current, retry_delay_seconds):
    if workflow.state == "ai_reviewing":
        _change_workflow(db, item, workflow, job, "ai_review_partial_failure")
    retry = retryable and job.attempt < job.max_attempts
    job.status = "retry_wait" if retry else "failed"
    job.last_error = code
    job.result = None
    job.claim_token = None
    job.lease_until = None
    job.available_at = current + timedelta(seconds=retry_delay_seconds)
    job.updated_at = current
    job.finished_at = None if retry else current
    _event(db, job, item, "review_job_retry_scheduled" if retry else "review_job_failed")
    _refresh(db, item)


def _matches(job, workflow, claim, current):
    return (
        job is not None
        and workflow is not None
        and job.delivery_id == claim.delivery_id
        and job.status == "running"
        and job.claim_token == claim.claim_token
        and job.attempt == claim.attempt
        and job.lease_until > current
        and job.workflow_revision == claim.workflow_revision == workflow.revision
        and workflow.state == "ai_reviewing"
    )


@_atomic
def claim_next(
    db: Session,
    *,
    timeout_seconds: float = 300,
    lease_grace_seconds: float = 5,
    max_attempts: int = 3,
    retry_delay_seconds: float = 5,
    now: datetime | None = None,
) -> JobClaim | None:
    if (
        timeout_seconds <= 0
        or lease_grace_seconds < 0
        or max_attempts < 1
        or retry_delay_seconds < 0
    ):
        raise ValueError("队列执行参数无效")
    current = _now(db, now)
    due = or_(
        and_(
            ReviewJob.status.in_(("queued", "retry_wait", "paused")),
            ReviewJob.available_at <= current,
        ),
        and_(ReviewJob.status == "running", ReviewJob.lease_until <= current),
    )
    candidates = list(
        db.scalars(
            select(ReviewJob.id).where(due).order_by(ReviewJob.available_at, ReviewJob.id).limit(20)
        )
    )
    with db.no_autoflush:
        for identity in candidates:
            locked = _lock(db, identity, skip_locked=True)
            if locked is None:
                continue
            item, workflow, job = locked
            # 获取锁后以数据库当前时钟再次检查，等待锁不会延长旧领取的有效期。
            current = _now(db, now)
            if job.status not in {"queued", "running", "retry_wait", "paused"}:
                continue
            if (
                workflow is None
                or workflow.state not in {"ai_reviewing", "ai_review_partial_failure"}
                or workflow.revision != job.workflow_revision
            ):
                job.status, job.last_error = "failed", "stale_workflow"
                job.claim_token = job.lease_until = None
                job.finished_at = job.updated_at = current
                _event(db, job, item, "review_job_discarded")
                _refresh(db, item)
                continue
            if job.status == "running":
                if job.lease_until <= current:
                    _event(db, job, item, "review_job_lease_expired")
                    _fail_locked(
                        db, item, workflow, job, "lease_expired", True, current, retry_delay_seconds
                    )
                continue
            if job.available_at > current:
                continue
            if job.attempt == 0:
                job.max_attempts = max_attempts
            if job.attempt >= job.max_attempts:
                _fail_locked(db, item, workflow, job, "execution_error", False, current, 0)
                continue
            job.attempt += 1
            if workflow.state == "ai_review_partial_failure":
                _change_workflow(db, item, workflow, job, "ai_reviewing")
            job.status, job.claim_token = "running", uuid4()
            job.lease_until = current + timedelta(seconds=timeout_seconds + lease_grace_seconds)
            job.started_at = job.updated_at = current
            job.finished_at = None
            _event(db, job, item, "review_job_claimed")
            _refresh(db, item)
            return JobClaim(
                job.id, item.id, item.project_id, job.claim_token, job.attempt, workflow.revision
            )
    return None


@_atomic
def complete_job(
    db: Session, claim: JobClaim, result: dict, *, now: datetime | None = None
) -> bool:
    if not isinstance(result, dict):
        raise ValueError("后台结果必须是JSON对象")
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 65536:
        raise ValueError("后台结果超过大小上限")
    with db.no_autoflush:
        locked = _lock(db, claim.job_id)
        if locked is None:
            return False
        item, workflow, job = locked
        current = _now(db, now)
        if item.project_id != claim.project_id or not _matches(job, workflow, claim, current):
            return False
        job.status, job.result = "succeeded", json.loads(encoded)
        job.claim_token = job.lease_until = None
        job.last_error = None
        job.finished_at = job.updated_at = current
        _event(db, job, item, "review_job_completed")
        _refresh(db, item)
        return True


@_atomic
def fail_job(
    db: Session,
    claim: JobClaim,
    error_code: str,
    *,
    retryable: bool = True,
    retry_delay_seconds: float = 5,
    retry_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    if error_code not in ERROR_CODES or retry_delay_seconds < 0:
        raise ValueError("后台失败代码或重试参数无效")
    with db.no_autoflush:
        locked = _lock(db, claim.job_id)
        if locked is None:
            return False
        item, workflow, job = locked
        current = _now(db, now)
        if item.project_id != claim.project_id or not _matches(job, workflow, claim, current):
            return False
        if error_code == "quota_exceeded":
            if workflow.state == "ai_reviewing":
                _change_workflow(db, item, workflow, job, "ai_review_partial_failure")
            if retry_at is not None and retry_at.tzinfo is None:
                raise ValueError("额度恢复时间必须带时区")
            job.status, job.last_error = "paused", "quota_exceeded"
            job.attempt -= 1
            job.claim_token = job.lease_until = None
            job.result = None
            job.available_at = retry_at or datetime.combine(
                current.date() + timedelta(days=1), datetime.min.time(), UTC
            )
            if job.available_at <= current:
                job.available_at = current + timedelta(seconds=60)
            job.updated_at = current
            job.finished_at = None
            _event(db, job, item, "review_job_paused")
            _refresh(db, item)
            return True
        _fail_locked(
            db,
            item,
            workflow,
            job,
            error_code,
            retryable and error_code not in {"pipeline_not_ready", "stale_workflow"},
            current,
            retry_delay_seconds,
        )
        return True


@_atomic
def retry_job(db: Session, delivery_id: UUID, *, now: datetime | None = None) -> ReviewJob:
    """可信业务层重试入口；本项不添加人/智能体的公开重试权限。"""
    identity = db.scalar(select(ReviewJob.id).where(ReviewJob.delivery_id == delivery_id))
    locked = _lock(db, identity) if identity is not None else None
    if locked is None:
        raise ReviewJobConflictError("审核任务不存在")
    item, workflow, job = locked
    if (
        job.status != "failed"
        or workflow is None
        or workflow.state != "ai_review_partial_failure"
        or workflow.revision != job.workflow_revision
    ):
        raise ReviewJobConflictError("审核任务当前不能重试")
    current = _now(db, now)
    job.status = "retry_wait"
    job.max_attempts += 3
    job.available_at = job.updated_at = current
    job.finished_at = None
    _event(db, job, item, "review_job_retry_scheduled")
    _refresh(db, item)
    return job
