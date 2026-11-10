"""智能体写入：复用协作规则，草稿独立于正式交付。"""

from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.agent_draft import AgentDraft
from app.models.collaboration import ProgressReport
from app.models.project import Project
from app.schemas.agent_drafts import AgentDraftUpload
from app.schemas.collaboration import MessageCreate, ProgressCreate, RequestCreate, RequestInfo
from app.services import agent_permissions, collaboration
from app.services.plans import get_latest_plan
from app.storage import Storage


class AgentActionValidationError(ValueError):
    pass


def _principal(
    db: Session,
    agent: Agent,
    action: str,
    *,
    project_id: UUID | None = None,
    module_id: UUID | None = None,
    request_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    with db.no_autoflush:
        agent_permissions.authorize(
            db, agent, action, project_id=project_id, module_id=module_id, request_id=request_id
        )
        identity = inspect(agent).identity
        agent_id = identity[0] if identity is not None else agent.id
        principal = db.execute(
            select(Agent.project_id, Agent.owner_id).where(Agent.id == agent_id)
        ).one_or_none()
        if principal is None:
            agent_permissions.deny(
                db,
                agent,
                action,
                "agent_inactive",
                project_id=project_id,
                module_id=module_id,
                request_id=request_id,
            )
        return agent_id, principal.project_id, principal.owner_id


def report_progress(
    db: Session,
    agent: Agent,
    module_id: UUID,
    body: ProgressCreate | dict,
    *,
    project_id: UUID | None = None,
) -> ProgressReport:
    _, project_id, owner_id = _principal(
        db, agent, "report_progress", project_id=project_id, module_id=module_id
    )
    return collaboration.report_progress(db, project_id, module_id, owner_id, body, agent=agent)


def _clean_filename(filename: str) -> str:
    if not isinstance(filename, str):
        raise AgentActionValidationError("请输入有效的文件名，长度不超过255个字符")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not filename
        or filename in (".", "..")
        or len(filename) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise AgentActionValidationError("请输入有效的文件名，长度不超过255个字符")
    return filename


def upload_draft(
    db: Session,
    agent: Agent,
    module_id: UUID,
    filename: str,
    stream: BinaryIO,
    handoff_note: str = "",
    *,
    storage: Storage,
    max_bytes: int,
    project_id: UUID | None = None,
) -> AgentDraft:
    agent_id, project_id, owner_id = _principal(
        db, agent, "upload_draft", project_id=project_id, module_id=module_id
    )
    filename = _clean_filename(filename)
    try:
        upload = AgentDraftUpload.model_validate({"handoff_note": handoff_note})
    except ValidationError:
        raise AgentActionValidationError("请填写有效的交接说明，长度不超过10000个字符") from None
    with db.no_autoflush:
        # 方案改派与本次上传采用相同的项目锁；随后重读实际身份和当前任务权限。
        db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
        db.scalar(
            select(Agent)
            .where(Agent.id == agent_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        agent_permissions.authorize(
            db, agent, "upload_draft", project_id=project_id, module_id=module_id
        )
        # 所属人变化时使用当前数据库主体，不能采用调用方缓存的属性。
        principal = db.execute(select(Agent.owner_id).where(Agent.id == agent_id)).one()
        owner_id = principal.owner_id
        plan = get_latest_plan(db, project_id)
        stored = storage.put(stream, max_bytes)
        item = AgentDraft(
            project_id=project_id,
            module_id=module_id,
            owner_id=owner_id,
            agent_id=agent_id,
            filename=filename,
            sha256=stored.sha256,
            size=stored.size,
            handoff_note=upload.handoff_note,
            created_at=datetime.now(UTC),
        )
        db.add(item)
        db.flush()
        record_event(
            db,
            project_id=project_id,
            actor_type="agent",
            actor_id=agent_id,
            represented_user_id=owner_id,
            event_type="agent_draft_uploaded",
            object_type="agent_draft",
            object_id=item.id,
            task_id=module_id,
            plan_version_id=plan.id,
            details={"draft_id": str(item.id), "status": item.status},
        )
        db.commit()
        return item


def create_request(
    db: Session,
    agent: Agent,
    body: RequestCreate | dict,
    *,
    project_id: UUID | None = None,
) -> RequestInfo:
    _, project_id, owner_id = _principal(db, agent, "create_request", project_id=project_id)
    return collaboration.create_request(db, project_id, owner_id, body, agent=agent)


def accept_request(
    db: Session,
    agent: Agent,
    request_id: UUID,
    *,
    project_id: UUID | None = None,
) -> RequestInfo:
    _, project_id, owner_id = _principal(
        db, agent, "accept_request", project_id=project_id, request_id=request_id
    )
    return collaboration.accept_request(db, project_id, request_id, owner_id, agent=agent)


def respond_request(
    db: Session,
    agent: Agent,
    request_id: UUID,
    body: MessageCreate | dict,
    *,
    project_id: UUID | None = None,
) -> RequestInfo:
    _, project_id, owner_id = _principal(
        db, agent, "respond_request", project_id=project_id, request_id=request_id
    )
    return collaboration.respond_request(db, project_id, request_id, owner_id, body, agent=agent)
