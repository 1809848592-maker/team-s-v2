from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import PlanVersion, User
from app.schemas.plans import PlanPublish, PlanVersionInfo
from app.services import modules, plans, projects

router = APIRouter(prefix="/projects/{project_id}/plan", tags=["plans"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def plan_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.LeaderRequiredError):
        return HTTPException(403, "只有项目负责人可以发布方案")
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    return HTTPException(404, "方案尚未发布或指定版本不存在")


@router.get("", response_model=PlanVersionInfo)
def latest_plan(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> PlanVersion:
    try:
        item = plans.get_plan(db, project_id, user.id)
    except (projects.ProjectError, plans.PlanNotFoundError) as error:
        raise plan_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/versions", response_model=list[PlanVersionInfo])
def plan_versions(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[PlanVersion]:
    try:
        items = plans.list_versions(db, project_id, user.id)
    except projects.ProjectError as error:
        raise plan_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.get("/versions/{version_id}", response_model=PlanVersionInfo)
def plan_version(
    project_id: UUID, version_id: UUID, user: CurrentUser, db: Database, response: Response
) -> PlanVersion:
    try:
        item = plans.get_plan(db, project_id, user.id, version_id)
    except (projects.ProjectError, plans.PlanNotFoundError) as error:
        raise plan_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/versions",
    status_code=201,
    response_model=PlanVersionInfo,
    dependencies=[Depends(require_csrf)],
)
def publish_plan(
    project_id: UUID, body: PlanPublish, user: CurrentUser, db: Database, response: Response
) -> PlanVersion:
    try:
        item = plans.publish_plan(db, project_id, user.id, body)
    except modules.ModuleValidationError as error:
        raise HTTPException(422, str(error)) from None
    except projects.ProjectError as error:
        raise plan_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item
