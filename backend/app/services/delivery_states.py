"""集中验证并记录审核状态转换。

这是供可信业务 service/worker 调用的内部机制，不暴露任意转换 HTTP/MCP 接口。
调用方负责相应操作的权限和审核意见判据；本层只处理状态图、并发版本及原子记录。
不提交事务，由正式提交、后台任务或终审 service 统一提交。
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.delivery import DeliveryVersion
from app.models.project import Project
from app.models.review_workflow import DeliveryStateTransition, DeliveryWorkflow
from app.schemas.review_state import ReviewState

_TRANSITIONS = {
    ReviewState.PENDING_CONFIRMATION: frozenset({ReviewState.AI_REVIEWING}),
    ReviewState.AI_REVIEWING: frozenset(
        {
            ReviewState.AI_REVIEW_PARTIAL_FAILURE,
            ReviewState.RETURNED,
            ReviewState.PENDING_FINAL_REVIEW,
        }
    ),
    ReviewState.AI_REVIEW_PARTIAL_FAILURE: frozenset({ReviewState.AI_REVIEWING}),
    ReviewState.PENDING_FINAL_REVIEW: frozenset({ReviewState.ADOPTED, ReviewState.FINAL_RETURNED}),
    # 退回后的重交产生新版本；采用后的重交要求不会改变旧版本状态。
    ReviewState.RETURNED: frozenset(),
    ReviewState.ADOPTED: frozenset(),
    ReviewState.FINAL_RETURNED: frozenset(),
}


class InvalidReviewTransitionError(ValueError):
    pass


class ReviewStateConflictError(Exception):
    pass


class ReviewDeliveryNotFoundError(Exception):
    pass


def validate_transition(source: ReviewState | str, target: ReviewState | str) -> ReviewState:
    try:
        source, target = ReviewState(source), ReviewState(target)
    except (ValueError, TypeError):
        raise InvalidReviewTransitionError("审核状态不存在") from None
    if target not in _TRANSITIONS[source]:
        raise InvalidReviewTransitionError("当前审核状态不允许此转换，请提交新版本或检查审核流程")
    return target


def _lock_delivery(db: Session, project_id: UUID, delivery_id: UUID) -> DeliveryVersion:
    # 始终与方案发布、本人确认和正式提交保持项目→工作流的锁序。
    if db.scalar(select(Project.id).where(Project.id == project_id).with_for_update()) is None:
        raise ReviewDeliveryNotFoundError
    item = db.scalar(
        select(DeliveryVersion).where(
            DeliveryVersion.id == delivery_id, DeliveryVersion.project_id == project_id
        )
    )
    if item is None:
        raise ReviewDeliveryNotFoundError
    return item


def _record(
    db: Session,
    item: DeliveryVersion,
    workflow: DeliveryWorkflow,
    previous: str | None,
    *,
    actor_id: UUID | str,
    actor_type: Literal["human", "agent"],
    represented_user_id: UUID | None,
    execution_context: dict | None = None,
) -> None:
    event = record_event(
        db,
        project_id=item.project_id,
        actor_id=actor_id,
        actor_type=actor_type,
        represented_user_id=represented_user_id,
        event_type="delivery_review_started"
        if previous is None
        else "delivery_review_state_changed",
        object_type="delivery_version",
        object_id=item.id,
        task_id=item.module_id,
        plan_version_id=item.plan_version_id,
        spec_version_id=item.spec_version_id,
        details={
            **(execution_context or {}),
            "from_state": previous,
            "to_state": workflow.state,
            "revision": workflow.revision,
        },
    )
    db.add(
        DeliveryStateTransition(
            delivery_id=item.id,
            revision=workflow.revision,
            from_state=previous,
            to_state=workflow.state,
            event_id=event.id,
        )
    )
    db.flush()
    # 只刷新只读关联，状态转换不改交付快照的任何字段。
    db.expire(item, ["review_workflow"])


def start_review(db: Session, project_id: UUID, delivery_id: UUID) -> DeliveryWorkflow:
    """已由人正式提交的版本在同一事务初始化状态和持久审核任务。"""
    try:
        with db.no_autoflush:
            item = _lock_delivery(db, project_id, delivery_id)
            if db.get(DeliveryWorkflow, delivery_id) is not None:
                raise ReviewStateConflictError("该交付已初始化审核，不能重复初始化")
            workflow = DeliveryWorkflow(
                delivery_id=item.id,
                state=ReviewState.AI_REVIEWING,
                revision=1,
                updated_at=datetime.now(UTC),
            )
            db.add(workflow)
            db.flush()
            _record(
                db,
                item,
                workflow,
                None,
                actor_id=item.submitted_by,
                actor_type="human",
                represented_user_id=None,
            )
            from app.services.review_jobs import enqueue_review

            enqueue_review(db, item, workflow)
            return workflow
    except Exception:
        db.rollback()
        raise


def transition_review(
    db: Session,
    project_id: UUID,
    delivery_id: UUID,
    target: ReviewState | str,
    *,
    expected_state: ReviewState | str,
    expected_revision: int,
    actor_id: UUID | str,
    actor_type: Literal["human", "agent"] = "human",
    represented_user_id: UUID | None = None,
    execution_context: dict | None = None,
) -> DeliveryWorkflow:
    """版本条件阻止迟到任务/重复决定；失败连同事件和历史一起回滚。"""
    try:
        target = validate_transition(expected_state, target)
        with db.no_autoflush:
            item = _lock_delivery(db, project_id, delivery_id)
            workflow = db.scalar(
                select(DeliveryWorkflow)
                .where(DeliveryWorkflow.delivery_id == item.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if workflow is None or (
                workflow.state != expected_state or workflow.revision != expected_revision
            ):
                raise ReviewStateConflictError("审核状态或修订号已变化，请重新读取")
            previous = workflow.state
            workflow.state = target
            workflow.revision += 1
            workflow.updated_at = datetime.now(UTC)
            _record(
                db,
                item,
                workflow,
                previous,
                actor_id=actor_id,
                actor_type=actor_type,
                represented_user_id=represented_user_id,
                execution_context=execution_context,
            )
            return workflow
    except Exception:
        db.rollback()
        raise
