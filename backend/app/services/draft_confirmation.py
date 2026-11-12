"""仅所属人可读私有草稿，并原子地确认成为正式交付。"""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.agent_draft import AgentDraft, DraftConfirmation
from app.models.delivery import DeliveryVersion
from app.models.project import Project, ProjectMember
from app.schemas.agent_drafts import AgentDraftInfo, AgentDraftView, DraftConfirm
from app.schemas.deliveries import DeliverySubmit
from app.schemas.plans import PlanContent
from app.services import deliveries
from app.services.plans import get_latest_plan
from app.services.projects import ProjectNotFoundError, require_member
from app.storage import Storage


class DraftNotFoundError(Exception):
    pass


class DraftAlreadyConfirmedError(Exception):
    pass


class DraftFileIntegrityError(Exception):
    pass


def _view(row) -> AgentDraftView:
    item, name, delivery_id, confirmed_at = row
    return AgentDraftView(
        **AgentDraftInfo.model_validate(item).model_dump(exclude={"status"}),
        status="confirmed" if delivery_id is not None else "pending_confirmation",
        agent_name=name,
        download_url=f"/api/projects/{item.project_id}/drafts/{item.id}/download",
        delivery_id=delivery_id,
        confirmed_at=confirmed_at,
    )


def _private_query(project_id: UUID, user_id: UUID):
    return (
        select(
            AgentDraft, Agent.name, DraftConfirmation.delivery_id, DraftConfirmation.confirmed_at
        )
        .join(Agent, Agent.id == AgentDraft.agent_id)
        .outerjoin(DraftConfirmation, DraftConfirmation.draft_id == AgentDraft.id)
        .where(AgentDraft.project_id == project_id, AgentDraft.owner_id == user_id)
    )


def list_drafts(
    db: Session, project_id: UUID, user_id: UUID, *, module_id: UUID | None = None
) -> list[AgentDraftView]:
    require_member(db, project_id, user_id)
    query = _private_query(project_id, user_id)
    if module_id is not None:
        query = query.where(AgentDraft.module_id == module_id)
    return [
        _view(row)
        for row in db.execute(query.order_by(AgentDraft.created_at.desc(), AgentDraft.id.desc()))
    ]


def get_draft(db: Session, project_id: UUID, draft_id: UUID, user_id: UUID) -> AgentDraftView:
    require_member(db, project_id, user_id)
    row = db.execute(
        _private_query(project_id, user_id).where(AgentDraft.id == draft_id)
    ).one_or_none()
    if row is None:
        raise DraftNotFoundError
    return _view(row)


def download_draft(
    db: Session, project_id: UUID, draft_id: UUID, user_id: UUID, *, storage: Storage
):
    item = get_draft(db, project_id, draft_id, user_id)
    path = storage.path(item.sha256)
    if not path.is_file():
        raise DraftNotFoundError
    return path, item.filename


def confirm_draft(
    db: Session,
    project_id: UUID,
    draft_id: UUID,
    user_id: UUID,
    body: DraftConfirm | dict,
    *,
    storage: Storage,
    max_bytes: int,
    review_return_limit: int = 3,
) -> DeliveryVersion:
    try:
        try:
            body = DraftConfirm.model_validate(body)
        except ValidationError:
            raise deliveries.DeliveryValidationError("请填写有效的 AI 参与声明及说明") from None
        with db.no_autoflush:
            # 与方案、规范发布及直接正式提交统一先锁项目，再锁原上传记录。
            if (
                db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
                is None
            ):
                raise ProjectNotFoundError
            member = db.scalar(
                select(ProjectMember.user_id).where(
                    ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
                )
            )
            if member is None:
                raise ProjectNotFoundError
            draft = db.scalar(
                select(AgentDraft)
                .where(
                    AgentDraft.id == draft_id,
                    AgentDraft.project_id == project_id,
                    AgentDraft.owner_id == user_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if draft is None:
                raise DraftNotFoundError
            if (
                db.scalar(
                    select(DraftConfirmation.id).where(DraftConfirmation.draft_id == draft.id)
                )
                is not None
            ):
                raise DraftAlreadyConfirmedError
            plan = get_latest_plan(db, project_id)
            if plan is None:
                raise deliveries.DeliveryValidationError("请先发布项目方案，再确认提交")
            module = next(
                (
                    item
                    for item in PlanContent.model_validate(plan.content).modules
                    if item.id == draft.module_id
                ),
                None,
            )
            if module is None or user_id not in module.assignee_ids:
                raise deliveries.DeliveryAssigneeRequiredError
            path = storage.path(draft.sha256)
            if not path.is_file():
                raise DraftNotFoundError
            with path.open("rb") as stream:
                delivery = deliveries.submit_delivery(
                    db,
                    project_id,
                    draft.module_id,
                    user_id,
                    draft.filename,
                    stream,
                    DeliverySubmit(
                        ai_participation=body.ai_participation,
                        ai_note=body.ai_note,
                        handoff_note=draft.handoff_note,
                        disclosure_version=body.disclosure_version,
                    ),
                    storage=storage,
                    max_bytes=max_bytes,
                    review_return_limit=review_return_limit,
                    commit=False,
                )
            if delivery.sha256 != draft.sha256 or delivery.size != draft.size:
                raise DraftFileIntegrityError
            confirmation = DraftConfirmation(
                draft_id=draft.id,
                delivery_id=delivery.id,
                confirmed_by=user_id,
                confirmed_at=datetime.now(UTC),
            )
            db.add(confirmation)
            db.flush()
            record_event(
                db,
                project_id=project_id,
                actor_id=user_id,
                event_type="agent_draft_confirmed",
                object_type="agent_draft",
                object_id=draft.id,
                task_id=draft.module_id,
                plan_version_id=delivery.plan_version_id,
                spec_version_id=delivery.spec_version_id,
                details={
                    "draft_id": str(draft.id),
                    "delivery_id": str(delivery.id),
                    "ai_participation": delivery.ai_participation,
                    "ai_note": delivery.ai_note,
                },
            )
            db.commit()
            return delivery
    except Exception:
        # 任何校验、存储或事件故障都不能留下已提交但未关联确认的交付。
        db.rollback()
        raise
