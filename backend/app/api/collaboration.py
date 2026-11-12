from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.schemas.bottlenecks import ProjectBottlenecks
from app.schemas.collaboration import (
    MessageCreate,
    ModuleProgress,
    ProgressCreate,
    ProgressInfo,
    RequestCreate,
    RequestInfo,
)
from app.services import bottlenecks, collaboration, projects

router = APIRouter(prefix="/projects/{project_id}", tags=["collaboration"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def collaboration_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    if isinstance(error, collaboration.CollaborationPermissionError):
        return HTTPException(403, str(error) or "你不能执行这项操作")
    if isinstance(error, collaboration.CollaborationStateError):
        return HTTPException(409, str(error) or "当前状态不支持这项操作，请刷新后重试")
    if isinstance(error, collaboration.CollaborationValidationError):
        return HTTPException(422, str(error))
    return HTTPException(404, "模块或协作需求不存在")


ERRORS = (
    projects.ProjectError,
    collaboration.CollaborationNotFoundError,
    collaboration.CollaborationPermissionError,
    collaboration.CollaborationStateError,
    collaboration.CollaborationValidationError,
)


@router.get("/bottlenecks", response_model=ProjectBottlenecks)
def project_bottlenecks(
    project_id: UUID, user: CurrentUser, db: Database, request: Request, response: Response
):
    try:
        result = bottlenecks.project_bottlenecks(
            db,
            project_id,
            user.id,
            return_after_days=request.app.state.settings.bottleneck_return_days,
        )
    except projects.ProjectError as error:
        result_error = project_error(error)
        result_error.headers = {**(result_error.headers or {}), "Cache-Control": "no-store"}
        raise result_error from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/modules/{module_id}/progress",
    status_code=201,
    response_model=ProgressInfo,
    dependencies=[Depends(require_csrf)],
)
def report_progress(
    project_id: UUID,
    module_id: UUID,
    body: ProgressCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.report_progress(db, project_id, module_id, user.id, body)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/modules/{module_id}/progress", response_model=list[ProgressInfo])
def module_progress(
    project_id: UUID,
    module_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        items = collaboration.list_progress(db, project_id, module_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.get("/progress", response_model=list[ModuleProgress])
def project_progress(project_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        items = collaboration.project_progress(db, project_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.post(
    "/requests",
    status_code=201,
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def create_request(
    project_id: UUID,
    body: RequestCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.create_request(db, project_id, user.id, body)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.get("/requests", response_model=list[RequestInfo])
def list_requests(project_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        items = collaboration.list_requests(db, project_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.get("/requests/{request_id}", response_model=RequestInfo)
def get_request(
    project_id: UUID,
    request_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.get_request(db, project_id, request_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/requests/{request_id}/accept",
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def accept_request(
    project_id: UUID,
    request_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.accept_request(db, project_id, request_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/requests/{request_id}/respond",
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def respond_request(
    project_id: UUID,
    request_id: UUID,
    body: MessageCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.respond_request(db, project_id, request_id, user.id, body)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/requests/{request_id}/supplement",
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def supplement_request(
    project_id: UUID,
    request_id: UUID,
    body: MessageCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.supplement_request(db, project_id, request_id, user.id, body)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item


@router.post(
    "/requests/{request_id}/resolve",
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def resolve_request(
    project_id: UUID,
    request_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        item = collaboration.resolve_request(db, project_id, request_id, user.id)
    except ERRORS as error:
        raise collaboration_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item
