"""动态呈现版本差异，负责人可留痕要求重交，原交付及审核始终保持原依据。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import (
    DeliveryResubmissionRequest,
    DeliveryVersion,
    DeliveryWorkflow,
    Notification,
    PlanVersion,
    Project,
    ProjectMember,
    SpecVersion,
)
from app.schemas.version_binding import (
    ResubmissionRequest,
    ResubmissionRequestInfo,
    VersionBindingInfo,
)
from app.services.deliveries import DeliveryNotFoundError
from app.services.plans import get_latest_plan
from app.services.projects import ProjectNotFoundError, require_member
from app.services.specs import get_latest_spec


class ResubmissionConflictError(ValueError):
    pass


def binding_info(
    db: Session, items: list[DeliveryVersion], *, leader: bool = False
) -> dict[UUID, VersionBindingInfo]:
    """仅供已完成可见性校验的调用方；按项目批量读取，避免每份交付逐项查询。"""
    if not items:
        return {}
    project_id = items[0].project_id
    if any(item.project_id != project_id for item in items):
        raise ValueError("绑定信息必须在同一个项目内读取")
    # 发布与提交持项目排他锁；这里共享锁固定一次列表读取的最新版本对。
    db.scalar(select(Project.id).where(Project.id == project_id).with_for_update(read=True))
    plan, spec = get_latest_plan(db, project_id), get_latest_spec(db, project_id)
    plan_versions = dict(
        db.execute(
            select(PlanVersion.id, PlanVersion.version).where(
                PlanVersion.project_id == project_id,
                PlanVersion.id.in_({item.plan_version_id for item in items}),
            )
        ).all()
    )
    spec_versions = dict(
        db.execute(
            select(SpecVersion.id, SpecVersion.version).where(
                SpecVersion.project_id == project_id,
                SpecVersion.id.in_({item.spec_version_id for item in items}),
            )
        ).all()
    )
    delivery_ids = [item.id for item in items]
    workflows = list(
        db.scalars(
            select(DeliveryWorkflow)
            .where(DeliveryWorkflow.delivery_id.in_(delivery_ids))
            .execution_options(populate_existing=True)
        )
    )
    states = {workflow.delivery_id: workflow.state for workflow in workflows}
    for item in items:
        db.expire(item, ["review_workflow"])
    requests = list(
        db.scalars(
            select(DeliveryResubmissionRequest)
            .where(
                DeliveryResubmissionRequest.project_id == project_id,
                DeliveryResubmissionRequest.delivery_id.in_(delivery_ids),
            )
            .order_by(
                DeliveryResubmissionRequest.requested_at.desc(),
                DeliveryResubmissionRequest.id.desc(),
            )
        )
    )
    latest_requests = {}
    for request in requests:
        latest_requests.setdefault(request.delivery_id, request)
    requested_targets = {
        (request.delivery_id, request.target_plan_version_id, request.target_spec_version_id)
        for request in requests
    }
    result = {}
    for item in items:
        plan_old, spec_old = item.plan_version_id != plan.id, item.spec_version_id != spec.id
        old_adopted = states.get(item.id) == "adopted" and (plan_old or spec_old)
        request = latest_requests.get(item.id)
        result[item.id] = VersionBindingInfo(
            bound_plan_version=plan_versions[item.plan_version_id],
            bound_spec_version=spec_versions[item.spec_version_id],
            latest_plan_version_id=plan.id,
            latest_spec_version_id=spec.id,
            latest_plan_version=plan.version,
            latest_spec_version=spec.version,
            plan_outdated=plan_old,
            spec_outdated=spec_old,
            based_on_old_version=old_adopted,
            can_request_resubmission=leader
            and old_adopted
            and (item.id, plan.id, spec.id) not in requested_targets,
            resubmission_request=ResubmissionRequestInfo.model_validate(request)
            if request
            else None,
        )
    return result


def request_resubmission(
    db: Session,
    project_id: UUID,
    delivery_id: UUID,
    user_id: UUID,
    body: ResubmissionRequest | dict,
) -> DeliveryVersion:
    """项目→工作流锁；请求、通知及事件同事务，重复点击同目标版本只记录一次。"""
    try:
        body = ResubmissionRequest.model_validate(body)
        with db.no_autoflush:
            require_member(db, project_id, user_id, leader=True)
            if (
                db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
                is None
            ):
                raise ProjectNotFoundError
            # 等锁后不沿用调用方缓存的旧角色。
            db.get(ProjectMember, (project_id, user_id), populate_existing=True)
            require_member(db, project_id, user_id, leader=True)
            item = db.scalar(
                select(DeliveryVersion)
                .where(
                    DeliveryVersion.project_id == project_id,
                    DeliveryVersion.id == delivery_id,
                )
                .execution_options(populate_existing=True)
            )
            if item is None:
                raise DeliveryNotFoundError
            state = db.scalar(
                select(DeliveryWorkflow.state)
                .where(DeliveryWorkflow.delivery_id == item.id)
                .with_for_update()
            )
            plan, spec = get_latest_plan(db, project_id), get_latest_spec(db, project_id)
            if (body.expected_plan_version_id, body.expected_spec_version_id) != (plan.id, spec.id):
                raise ResubmissionConflictError("方案或规范已更新，请刷新后重试")
            if state != "adopted" or (item.plan_version_id, item.spec_version_id) == (
                plan.id,
                spec.id,
            ):
                raise ResubmissionConflictError("仅依据旧版的已采用交付可要求重新提交")
            existing = db.scalar(
                select(DeliveryResubmissionRequest.id).where(
                    DeliveryResubmissionRequest.delivery_id == item.id,
                    DeliveryResubmissionRequest.target_plan_version_id == plan.id,
                    DeliveryResubmissionRequest.target_spec_version_id == spec.id,
                )
            )
            if existing is None:
                request_id, requested_at = uuid4(), datetime.now(UTC)
                event = record_event(
                    db,
                    project_id=project_id,
                    actor_id=user_id,
                    event_type="delivery_resubmission_requested",
                    object_type="delivery_version",
                    object_id=item.id,
                    task_id=item.module_id,
                    plan_version_id=item.plan_version_id,
                    spec_version_id=item.spec_version_id,
                    details={
                        "request_id": str(request_id),
                        "version": item.version,
                        "target_plan_version_id": str(plan.id),
                        "target_spec_version_id": str(spec.id),
                        "target_plan_version": plan.version,
                        "target_spec_version": spec.version,
                    },
                )
                db.add(
                    DeliveryResubmissionRequest(
                        id=request_id,
                        project_id=project_id,
                        delivery_id=item.id,
                        requested_by=user_id,
                        target_plan_version_id=plan.id,
                        target_spec_version_id=spec.id,
                        event_id=event.id,
                        requested_at=requested_at,
                    )
                )
                db.add(
                    Notification(
                        project_id=project_id,
                        user_id=item.submitted_by,
                        kind="delivery_resubmission_requested",
                        message=(
                            f"负责人要求按方案第 {plan.version} 版、"
                            f"规范第 {spec.version} 版重新提交交付"
                        ),
                        details={
                            "request_id": str(request_id),
                            "delivery_id": str(item.id),
                            "module_id": str(item.module_id),
                            "plan_version_id": str(plan.id),
                            "spec_version_id": str(spec.id),
                        },
                        created_at=requested_at,
                    )
                )
            db.commit()
            return item
    except Exception:
        db.rollback()
        raise
