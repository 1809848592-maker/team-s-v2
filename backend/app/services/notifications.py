from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import Notification, PlanVersion, ProjectMember, SpecVersion
from app.schemas.plans import ModuleContent


class NotificationNotFoundError(Exception):
    pass


def notify_plan_change(
    db: Session,
    plan: PlanVersion,
    affected: list[tuple[ModuleContent | None, ModuleContent | None]],
) -> None:
    recipients: dict[UUID, set[str]] = {}
    for old, new in affected:
        for module in (old, new):
            if module is not None:
                for user_id in module.assignee_ids:
                    recipients.setdefault(user_id, set()).add(str(module.id))
    for user_id, module_ids in recipients.items():
        db.add(
            Notification(
                project_id=plan.project_id,
                user_id=user_id,
                kind="plan_updated",
                message=f"方案已发布第 {plan.version} 版，你负责的分工模块有更新",
                details={
                    "plan_version_id": str(plan.id),
                    "version": plan.version,
                    "module_ids": sorted(module_ids),
                },
                created_at=datetime.now(UTC),
            )
        )


def list_notifications(
    db: Session, user_id: UUID, project_id: UUID | None = None
) -> list[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if project_id is not None:
        query = query.where(Notification.project_id == project_id)
    return list(db.scalars(query.order_by(Notification.created_at.desc(), Notification.id.desc())))


def notify_spec_change(db: Session, spec: SpecVersion) -> None:
    for user_id in db.scalars(
        select(ProjectMember.user_id).where(ProjectMember.project_id == spec.project_id)
    ):
        db.add(
            Notification(
                project_id=spec.project_id,
                user_id=user_id,
                kind="spec_updated",
                message=f"团队规范已发布第 {spec.version} 版",
                details={"spec_version_id": str(spec.id), "version": spec.version},
                created_at=datetime.now(UTC),
            )
        )


def read_notification(db: Session, notification_id: UUID, user_id: UUID) -> Notification:
    item = db.scalar(
        select(Notification)
        .where(Notification.id == notification_id, Notification.user_id == user_id)
        .with_for_update()
    )
    if item is None:
        raise NotificationNotFoundError
    if item.read_at is None:
        item.read_at = datetime.now(UTC)
        plan_version_id = item.details.get("plan_version_id")
        spec_version_id = item.details.get("spec_version_id")
        record_event(
            db,
            project_id=item.project_id,
            actor_id=user_id,
            event_type="notification_read",
            object_type="notification",
            object_id=item.id,
            plan_version_id=UUID(plan_version_id) if plan_version_id else None,
            spec_version_id=UUID(spec_version_id) if spec_version_id else None,
            details={"kind": item.kind},
        )
        db.commit()
    return item
