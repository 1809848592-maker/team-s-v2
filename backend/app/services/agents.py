import re
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events import record_event
from app.models.agent import Agent
from app.models.project import Project, ProjectMember
from app.schemas.agents import AgentCreate
from app.security import new_token, token_hash
from app.services.projects import require_member

_TOKEN_PREFIX = "tsv2_agent_"
_TOKEN_PATTERN = re.compile(r"tsv2_agent_[A-Za-z0-9_-]{43}")


class AgentNotFoundError(Exception):
    pass


class AgentValidationError(ValueError):
    pass


class AgentUnauthenticatedError(Exception):
    pass


def register_agent(
    db: Session, project_id: UUID, user_id: UUID, body: AgentCreate | dict
) -> tuple[Agent, str]:
    require_member(db, project_id, user_id)
    try:
        body = AgentCreate.model_validate(body)
    except ValidationError:
        raise AgentValidationError("请填写有效的智能体名称和客户端类型") from None
    raw_token = _TOKEN_PREFIX + new_token()
    item = Agent(
        project_id=project_id,
        owner_id=user_id,
        name=body.name,
        client_type=body.client_type,
        token_hash=token_hash(raw_token),
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    record_event(
        db,
        project_id=project_id,
        actor_id=user_id,
        event_type="agent_registered",
        object_type="agent",
        object_id=item.id,
        details={"name": item.name, "client_type": item.client_type},
    )
    db.commit()
    return item, raw_token


def list_agents(db: Session, project_id: UUID, user_id: UUID) -> list[Agent]:
    require_member(db, project_id, user_id)
    return list(
        db.scalars(
            select(Agent)
            .where(Agent.project_id == project_id, Agent.owner_id == user_id)
            .order_by(Agent.created_at.desc(), Agent.id.desc())
            .execution_options(populate_existing=True)
        )
    )


def revoke_agent(db: Session, project_id: UUID, agent_id: UUID, user_id: UUID) -> Agent:
    require_member(db, project_id, user_id)
    # 智能体写操作按项目→智能体顺序持锁；吊销沿用该顺序，避免与调用审计外键形成锁环。
    db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
    item = db.scalar(
        select(Agent)
        .where(Agent.id == agent_id, Agent.project_id == project_id, Agent.owner_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise AgentNotFoundError
    if item.revoked_at is None:
        item.revoked_at = datetime.now(UTC)
        record_event(
            db,
            project_id=project_id,
            actor_id=user_id,
            event_type="agent_revoked",
            object_type="agent",
            object_id=item.id,
            details={"name": item.name, "client_type": item.client_type},
        )
        db.commit()
    return item


def authenticate_agent(db: Session, raw_token: str | None) -> Agent:
    if not isinstance(raw_token, str) or _TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise AgentUnauthenticatedError
    # 每次查询数据库中的吊销状态及当前成员关系，不以缓存的ORM对象作为认证依据。
    item = db.scalar(
        select(Agent)
        .join(
            ProjectMember,
            (ProjectMember.project_id == Agent.project_id)
            & (ProjectMember.user_id == Agent.owner_id),
        )
        .where(Agent.token_hash == token_hash(raw_token), Agent.revoked_at.is_(None))
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise AgentUnauthenticatedError
    return item
