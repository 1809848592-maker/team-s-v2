from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import PlanVersion, Project
from app.schemas.plans import PlanPublish
from app.services import modules, notifications
from app.services.projects import ProjectNotFoundError, require_member


class PlanNotFoundError(Exception):
    pass


def get_latest_plan(db: Session, project_id: UUID) -> PlanVersion | None:
    """仅内部调用；公开读取接口必须先校验项目成员身份。"""
    return db.scalar(
        select(PlanVersion)
        .where(PlanVersion.project_id == project_id)
        .order_by(PlanVersion.version.desc())
        .limit(1)
    )


def get_plan(
    db: Session, project_id: UUID, user_id: UUID, version_id: UUID | None = None
) -> PlanVersion:
    require_member(db, project_id, user_id)
    if version_id is None:
        item = get_latest_plan(db, project_id)
    else:
        item = db.scalar(
            select(PlanVersion).where(
                PlanVersion.project_id == project_id, PlanVersion.id == version_id
            )
        )
    if item is None:
        raise PlanNotFoundError
    return item


def list_versions(db: Session, project_id: UUID, user_id: UUID) -> list[PlanVersion]:
    require_member(db, project_id, user_id)
    return list(
        db.scalars(
            select(PlanVersion)
            .where(PlanVersion.project_id == project_id)
            .order_by(PlanVersion.version.desc())
        )
    )


def publish_plan(
    db: Session, project_id: UUID, user_id: UUID, publication: PlanPublish | dict
) -> PlanVersion:
    require_member(db, project_id, user_id, leader=True)
    publication = PlanPublish.model_validate(publication)
    # 所有发布者锁同一项目；等待锁后再读最新版本，避免并发发布争用同一版本号。
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise ProjectNotFoundError
    previous = get_latest_plan(db, project_id)
    modules.validate_modules(db, project_id, publication.content.modules)
    affected = modules.affected_modules(previous, publication.content)
    item = PlanVersion(
        project_id=project_id,
        version=previous.version + 1 if previous is not None else 1,
        change_note=publication.change_note,
        content=publication.content.model_dump(mode="json"),
        published_by=user_id,
        published_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="plan_published",
        object_type="plan_version",
        object_id=item.id,
        plan_version_id=item.id,
        details={"version": item.version, "change_note": item.change_note},
    )
    notifications.notify_plan_change(db, item, affected)
    for old, new in affected:
        module = new if new is not None else old
        record_event(
            db,
            project_id=project_id,
            actor_id=user_id,
            event_type="module_updated",
            object_type="module",
            object_id=module.id,
            task_id=module.id,
            plan_version_id=item.id,
            details={"version": item.version, "removed": new is None},
        )
    db.commit()
    return item
