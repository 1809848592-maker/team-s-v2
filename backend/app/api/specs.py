from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import SpecVersion, User
from app.schemas.specs import SpecPublish, SpecVersionInfo
from app.services import projects, specs

router = APIRouter(prefix="/projects/{project_id}/spec", tags=["specs"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def spec_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.LeaderRequiredError):
        return HTTPException(403, "只有项目负责人可以发布规范")
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    return HTTPException(404, "规范尚未发布或指定版本不存在")


@router.get("", response_model=SpecVersionInfo)
def latest_spec(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> SpecVersion:
    try:
        item = specs.get_spec(db, project_id, user.id)
    except (projects.ProjectError, specs.SpecNotFoundError) as error:
        raise spec_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/versions", response_model=list[SpecVersionInfo])
def spec_versions(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[SpecVersion]:
    try:
        items = specs.list_versions(db, project_id, user.id)
    except projects.ProjectError as error:
        raise spec_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.get("/versions/{version_id}", response_model=SpecVersionInfo)
def spec_version(
    project_id: UUID, version_id: UUID, user: CurrentUser, db: Database, response: Response
) -> SpecVersion:
    try:
        item = specs.get_spec(db, project_id, user.id, version_id)
    except (projects.ProjectError, specs.SpecNotFoundError) as error:
        raise spec_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/versions",
    status_code=201,
    response_model=SpecVersionInfo,
    dependencies=[Depends(require_csrf)],
)
def publish_spec(
    project_id: UUID, body: SpecPublish, user: CurrentUser, db: Database, response: Response
) -> SpecVersion:
    try:
        item = specs.publish_spec(db, project_id, user.id, body)
    except specs.SpecValidationError as error:
        raise HTTPException(422, str(error)) from None
    except projects.ProjectError as error:
        raise spec_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item
