from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.delivery import DeliveryVersion, SharedMaterial
from app.models.module import Module
from app.models.project import Project
from app.schemas.deliveries import DeliverySubmit
from app.schemas.plans import PlanContent
from app.services.plans import get_latest_plan
from app.services.projects import ProjectNotFoundError, require_member
from app.services.specs import get_latest_spec
from app.storage import Storage


class DeliveryNotFoundError(Exception):
    pass


class MaterialNotFoundError(Exception):
    pass


class DeliveryAssigneeRequiredError(Exception):
    pass


class DeliveryValidationError(ValueError):
    pass


def _clean_filename(filename: str) -> str:
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not filename
        or filename in (".", "..")
        or len(filename) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise DeliveryValidationError("请输入有效的文件名，长度不超过255个字符")
    return filename


def _require_module(db: Session, project_id: UUID, module_id: UUID) -> Module:
    module = db.scalar(
        select(Module).where(Module.id == module_id, Module.project_id == project_id)
    )
    if module is None:
        raise DeliveryNotFoundError
    return module


def submit_delivery(
    db: Session,
    project_id: UUID,
    module_id: UUID,
    user_id: UUID,
    filename: str,
    stream: BinaryIO,
    submission: DeliverySubmit | dict,
    *,
    storage: Storage,
    max_bytes: int,
    commit: bool = True,
) -> DeliveryVersion:
    require_member(db, project_id, user_id)
    try:
        submission = DeliverySubmit.model_validate(submission)
    except ValidationError:
        raise DeliveryValidationError("请填写有效的 AI 参与声明及交接说明") from None
    filename = _clean_filename(filename)
    # 方案、规范和交付均锁项目行，随后读取的绑定版本与模块权限属于同一提交时点。
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None:
        raise ProjectNotFoundError
    plan = get_latest_plan(db, project_id)
    if plan is None:
        raise DeliveryValidationError("请先发布项目方案，再提交正式交付")
    _require_module(db, project_id, module_id)
    module = next(
        (item for item in PlanContent.model_validate(plan.content).modules if item.id == module_id),
        None,
    )
    if module is None:
        raise DeliveryNotFoundError
    if user_id not in module.assignee_ids:
        raise DeliveryAssigneeRequiredError
    spec = get_latest_spec(db, project_id)
    if spec is None:
        raise DeliveryValidationError("请先发布团队规范，再提交正式交付")
    previous_version = db.scalar(
        select(DeliveryVersion.version)
        .where(DeliveryVersion.project_id == project_id, DeliveryVersion.module_id == module_id)
        .order_by(DeliveryVersion.version.desc())
        .limit(1)
    )
    stored = storage.put(stream, max_bytes)
    item = DeliveryVersion(
        project_id=project_id,
        module_id=module_id,
        version=(previous_version or 0) + 1,
        filename=filename,
        sha256=stored.sha256,
        size=stored.size,
        submitted_by=user_id,
        submitted_at=datetime.now(UTC),
        plan_version_id=plan.id,
        spec_version_id=spec.id,
        ai_participation=submission.ai_participation,
        ai_note=submission.ai_note,
        handoff_note=submission.handoff_note,
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="delivery_submitted",
        object_type="delivery_version",
        object_id=item.id,
        task_id=module_id,
        plan_version_id=plan.id,
        spec_version_id=spec.id,
        details={
            "version": item.version,
            "filename": item.filename,
            "sha256": item.sha256,
            "size": item.size,
            "ai_participation": item.ai_participation,
            "ai_note": item.ai_note,
            "handoff_note": item.handoff_note,
        },
    )
    if commit:
        db.commit()
    return item


def list_deliveries(
    db: Session, project_id: UUID, user_id: UUID, *, module_id: UUID | None = None
) -> list[DeliveryVersion]:
    require_member(db, project_id, user_id)
    query = select(DeliveryVersion).where(DeliveryVersion.project_id == project_id)
    if module_id is not None:
        # 使用稳定标识保留已从新版方案移除的模块及其历史交付的可见性。
        _require_module(db, project_id, module_id)
        query = query.where(DeliveryVersion.module_id == module_id)
        query = query.order_by(DeliveryVersion.version.desc())
    else:
        query = query.order_by(DeliveryVersion.submitted_at.desc(), DeliveryVersion.id.desc())
    return list(db.scalars(query))


def get_delivery(
    db: Session, project_id: UUID, module_id: UUID, user_id: UUID, delivery_id: UUID
) -> DeliveryVersion:
    require_member(db, project_id, user_id)
    item = db.scalar(
        select(DeliveryVersion).where(
            DeliveryVersion.id == delivery_id,
            DeliveryVersion.project_id == project_id,
            DeliveryVersion.module_id == module_id,
        )
    )
    if item is None:
        raise DeliveryNotFoundError
    return item


def upload_material(
    db: Session,
    project_id: UUID,
    user_id: UUID,
    filename: str,
    stream: BinaryIO,
    *,
    storage: Storage,
    max_bytes: int,
) -> SharedMaterial:
    require_member(db, project_id, user_id)
    filename = _clean_filename(filename)
    stored = storage.put(stream, max_bytes)
    item = SharedMaterial(
        project_id=project_id,
        filename=filename,
        sha256=stored.sha256,
        size=stored.size,
        uploaded_by=user_id,
        uploaded_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="material_uploaded",
        object_type="shared_material",
        object_id=item.id,
        details={"filename": item.filename, "sha256": item.sha256, "size": item.size},
    )
    db.commit()
    return item


def list_materials(db: Session, project_id: UUID, user_id: UUID) -> list[SharedMaterial]:
    require_member(db, project_id, user_id)
    return list(
        db.scalars(
            select(SharedMaterial)
            .where(SharedMaterial.project_id == project_id)
            .order_by(SharedMaterial.uploaded_at.desc(), SharedMaterial.id.desc())
        )
    )


def get_material(db: Session, project_id: UUID, user_id: UUID, material_id: UUID) -> SharedMaterial:
    require_member(db, project_id, user_id)
    item = db.scalar(
        select(SharedMaterial).where(
            SharedMaterial.id == material_id, SharedMaterial.project_id == project_id
        )
    )
    if item is None:
        raise MaterialNotFoundError
    return item
