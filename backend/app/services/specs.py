from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models import Project, SpecVersion
from app.schemas.specs import SpecPublish
from app.services import notifications, resources
from app.services.projects import ProjectNotFoundError, require_member


class SpecNotFoundError(Exception):
    pass


class SpecValidationError(ValueError):
    pass


def get_latest_spec(db: Session, project_id: UUID) -> SpecVersion | None:
    """仅内部调用；公开读取接口必须先校验项目成员身份。"""
    return db.scalar(
        select(SpecVersion)
        .where(SpecVersion.project_id == project_id)
        .order_by(SpecVersion.version.desc())
        .limit(1)
    )


def get_spec(
    db: Session, project_id: UUID, user_id: UUID, version_id: UUID | None = None
) -> SpecVersion:
    require_member(db, project_id, user_id)
    if version_id is None:
        item = get_latest_spec(db, project_id)
    else:
        item = db.scalar(
            select(SpecVersion).where(
                SpecVersion.project_id == project_id, SpecVersion.id == version_id
            )
        )
    if item is None:
        raise SpecNotFoundError
    return item


def list_versions(db: Session, project_id: UUID, user_id: UUID) -> list[SpecVersion]:
    require_member(db, project_id, user_id)
    return list(
        db.scalars(
            select(SpecVersion)
            .where(SpecVersion.project_id == project_id)
            .order_by(SpecVersion.version.desc())
        )
    )


def publish_spec(
    db: Session, project_id: UUID, user_id: UUID, publication: SpecPublish | dict
) -> SpecVersion:
    require_member(db, project_id, user_id, leader=True)
    publication = SpecPublish.model_validate(publication)
    # 与方案发布锁相同的项目行，保证并发发布分配到连续且不重复的版本号。
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise ProjectNotFoundError
    previous = get_latest_spec(db, project_id)
    for resource_id in {item.template_resource_id for item in publication.items} - {None}:
        try:
            resources.resolve_resource(db, project_id, user_id, resource_id)
        except resources.ResourceNotFoundError:
            raise SpecValidationError("规范模板必须来自当前项目或内置范例库") from None
    item = SpecVersion(
        project_id=project_id,
        version=previous.version + 1 if previous is not None else 1,
        change_note=publication.change_note,
        items=[entry.model_dump(mode="json") for entry in publication.items],
        published_by=user_id,
        published_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="spec_published",
        object_type="spec_version",
        object_id=item.id,
        spec_version_id=item.id,
        details={"version": item.version, "change_note": item.change_note},
    )
    notifications.notify_spec_change(db, item)
    db.commit()
    return item
