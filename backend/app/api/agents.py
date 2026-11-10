from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.events import record_event
from app.models.agent import Agent
from app.models.auth import User
from app.schemas.agents import AgentCreate, AgentCreated, AgentInfo
from app.services import agents, projects

router = APIRouter(tags=["agents"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
agent_bearer = HTTPBearer(auto_error=False, scheme_name="AgentBearer")
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(agent_bearer)]


def agent_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.ProjectError):
        result = project_error(error)
        result.headers = {**(result.headers or {}), "Cache-Control": "no-store"}
        return result
    if isinstance(error, agents.AgentValidationError):
        return HTTPException(422, str(error), headers={"Cache-Control": "no-store"})
    return HTTPException(404, "智能体不存在或不属于当前账号", headers={"Cache-Control": "no-store"})


def require_agent(request: Request, db: Database, bearer: BearerCredentials) -> Agent:
    # 独立 Bearer 身份通道；人类会话 Cookie 不参与智能体身份认证。
    try:
        return agents.authenticate_agent(db, bearer.credentials if bearer else None)
    except agents.AgentUnauthenticatedError:
        raise HTTPException(
            401,
            "智能体令牌无效或已吊销",
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        ) from None


CurrentAgent = Annotated[Agent, Depends(require_agent)]
ERRORS = (projects.ProjectError, agents.AgentNotFoundError, agents.AgentValidationError)


@router.get("/projects/{project_id}/agents", response_model=list[AgentInfo])
def list_agents(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[Agent]:
    try:
        items = agents.list_agents(db, project_id, user.id)
    except ERRORS as error:
        raise agent_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.post(
    "/projects/{project_id}/agents",
    status_code=201,
    response_model=AgentCreated,
    dependencies=[Depends(require_csrf)],
)
def register_agent(
    project_id: UUID,
    body: AgentCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> AgentCreated:
    try:
        item, token = agents.register_agent(db, project_id, user.id, body)
    except ERRORS as error:
        raise agent_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return AgentCreated(agent=AgentInfo.model_validate(item), token=token)


@router.post(
    "/projects/{project_id}/agents/{agent_id}/revoke",
    response_model=AgentInfo,
    dependencies=[Depends(require_csrf)],
)
def revoke_agent(
    project_id: UUID,
    agent_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> Agent:
    try:
        item = agents.revoke_agent(db, project_id, agent_id, user.id)
    except ERRORS as error:
        raise agent_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/agent/me", response_model=AgentInfo)
def agent_me(agent: CurrentAgent, db: Database, response: Response) -> Agent:
    record_event(
        db,
        project_id=agent.project_id,
        actor_type="agent",
        actor_id=agent.id,
        represented_user_id=agent.owner_id,
        event_type="agent_identity_read",
        object_type="agent",
        object_id=agent.id,
    )
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return agent
