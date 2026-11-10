from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.events import record_event
from app.models.resource import Resource
from app.presets.catalog import BUILTIN_TEMPLATES
from app.services.projects import require_member
from app.storage import LocalStorage, Storage


class ResourceNotFoundError(Exception):
    pass


class ResourceValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    id: UUID
    filename: str
    sha256: str
    size: int
    builtin: bool
    path: Path


def _storage(storage: Storage | None) -> Storage:
    return storage if storage is not None else LocalStorage(get_settings().storage_dir)


def _resource_info(resource: Resource, storage: Storage) -> ResolvedResource:
    return ResolvedResource(
        id=resource.id,
        filename=resource.filename,
        sha256=resource.sha256,
        size=resource.size,
        builtin=False,
        path=storage.path(resource.sha256),
    )


def builtin_resources() -> list[ResolvedResource]:
    return [
        ResolvedResource(
            id=item.id,
            filename=item.filename,
            sha256=item.sha256,
            size=item.size,
            builtin=True,
            path=item.path,
        )
        for item in BUILTIN_TEMPLATES
    ]


def resolve_resource(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    resource_id: UUID,
    *,
    storage: Storage | None = None,
) -> ResolvedResource:
    require_member(db, project_id, user_id)
    for item in builtin_resources():
        if item.id == resource_id:
            return item
    resource = db.scalar(
        select(Resource).where(Resource.id == resource_id, Resource.project_id == project_id)
    )
    if resource is None:
        raise ResourceNotFoundError
    return _resource_info(resource, _storage(storage))


def list_resources(
    db: Session, project_id: UUID, user_id: UUID, *, storage: Storage | None = None
) -> list[ResolvedResource]:
    require_member(db, project_id, user_id)
    resources = db.scalars(
        select(Resource)
        .where(Resource.project_id == project_id)
        .order_by(Resource.created_at.desc(), Resource.id.desc())
    )
    local = _storage(storage)
    return builtin_resources() + [_resource_info(item, local) for item in resources]


def upload_resource(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    filename: str,
    stream: BinaryIO,
    *,
    storage: Storage,
    max_bytes: int,
) -> ResolvedResource:
    require_member(db, project_id, user_id, leader=True)
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not filename
        or filename in (".", "..")
        or len(filename) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise ResourceValidationError("请输入有效的文件名，长度不超过255个字符")
    stored = storage.put(stream, max_bytes)
    resource = Resource(
        project_id=project_id,
        filename=filename,
        sha256=stored.sha256,
        size=stored.size,
        created_by=user_id,
    )
    db.add(resource)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="template_uploaded",
        object_type="resource",
        object_id=resource.id,
        details={"filename": resource.filename, "sha256": stored.sha256, "size": stored.size},
    )
    db.commit()
    return _resource_info(resource, storage)
