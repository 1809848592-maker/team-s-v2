"""负责人看板的只读卡点计算；不改变交付、需求或进度。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.collaboration import CollaborationRequest
from app.models.delivery import DeliveryVersion
from app.models.review_workflow import DeliveryStateTransition, DeliveryWorkflow
from app.schemas.bottlenecks import Bottleneck, ProjectBottlenecks
from app.schemas.plans import PlanContent
from app.services.plans import get_latest_plan
from app.services.projects import require_member


def _now() -> datetime:
    return datetime.now(UTC)


def project_bottlenecks(
    db: Session, project_id: UUID, user_id: UUID, *, return_after_days: int
) -> ProjectBottlenecks:
    require_member(db, project_id, user_id, leader=True)
    now = _now()
    plan = get_latest_plan(db, project_id)
    modules = PlanContent.model_validate(plan.content).modules if plan is not None else []
    module_ids = [module.id for module in modules]
    latest = {
        delivery.module_id: (delivery, state, returned_at)
        for delivery, state, returned_at in db.execute(
            select(DeliveryVersion, DeliveryWorkflow.state, DeliveryStateTransition.created_at)
            .outerjoin(DeliveryWorkflow, DeliveryWorkflow.delivery_id == DeliveryVersion.id)
            .outerjoin(
                DeliveryStateTransition,
                and_(
                    DeliveryStateTransition.delivery_id == DeliveryVersion.id,
                    DeliveryStateTransition.revision == DeliveryWorkflow.revision,
                    DeliveryStateTransition.to_state == DeliveryWorkflow.state,
                ),
            )
            .where(
                DeliveryVersion.project_id == project_id,
                DeliveryVersion.module_id.in_(module_ids),
            )
            .distinct(DeliveryVersion.module_id)
            .order_by(DeliveryVersion.module_id, DeliveryVersion.version.desc())
        )
    }
    items = []
    for module in modules:
        current = latest.get(module.id)
        if current is None:
            if module.deadline is not None and module.deadline < now:
                items.append(
                    Bottleneck(
                        kind="submission_overdue",
                        module_id=module.id,
                        title=module.name,
                        since=module.deadline,
                    )
                )
            continue
        delivery, state, returned_at = current
        if (
            state in {"returned", "final_returned"}
            and returned_at is not None
            and (now - returned_at).total_seconds() > return_after_days * 86400
        ):
            # 只有已经超过阈值才计算日期，极大的正整数配置不会造成日期溢出。
            items.append(
                Bottleneck(
                    kind="resubmission_overdue",
                    module_id=module.id,
                    delivery_id=delivery.id,
                    title=module.name,
                    since=returned_at + timedelta(days=return_after_days),
                )
            )
    requests = db.scalars(
        select(CollaborationRequest)
        .where(
            CollaborationRequest.project_id == project_id,
            CollaborationRequest.status.in_(["open", "accepted"]),
            CollaborationRequest.expected_by < now,
        )
        .order_by(CollaborationRequest.expected_by, CollaborationRequest.id)
    )
    for request in requests:
        items.append(
            Bottleneck(
                kind="request_overdue",
                module_id=request.target_module_id,
                request_id=request.id,
                title=request.title,
                since=request.expected_by,
            )
        )
    return ProjectBottlenecks(
        project_id=project_id,
        evaluated_at=now,
        return_after_days=return_after_days,
        items=items,
    )
