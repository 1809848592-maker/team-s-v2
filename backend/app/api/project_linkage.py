"""人类项目成员读取报告，项目负责人触发审核和分派意见。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.collaboration import ERRORS as COLLABORATION_ERRORS
from app.api.collaboration import collaboration_error
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.schemas.collaboration import RequestCreate, RequestInfo
from app.schemas.project_linkage import ProjectLinkageInfo
from app.services import model_disclosure, project_linkage, projects

router = APIRouter(prefix="/projects/{project_id}/linkage-reviews", tags=["project linkage"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]
HEADERS = {"Cache-Control": "no-store"}
ERRORS = (
    *COLLABORATION_ERRORS,
    project_linkage.ProjectLinkageNotFoundError,
    project_linkage.ProjectLinkageConflictError,
    model_disclosure.ModelConsentRequiredError,
    model_disclosure.DisclosureVersionError,
)


def _error(error: Exception) -> HTTPException:
    if isinstance(error, project_linkage.ProjectLinkageNotFoundError):
        result = HTTPException(404, "衔接审核或意见不存在")
    elif isinstance(
        error,
        (
            project_linkage.ProjectLinkageConflictError,
            model_disclosure.ModelConsentRequiredError,
            model_disclosure.DisclosureVersionError,
        ),
    ):
        result = HTTPException(409, str(error))
    elif isinstance(error, projects.LeaderRequiredError):
        result = HTTPException(403, "只有项目负责人可以执行完整衔接审核和分派意见")
    elif isinstance(error, projects.ProjectError):
        result = project_error(error)
    else:
        result = collaboration_error(error)
    result.headers = {**(result.headers or {}), **HEADERS}
    return result


@router.post(
    "", status_code=201, response_model=ProjectLinkageInfo, dependencies=[Depends(require_csrf)]
)
def start(project_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        result = project_linkage.start_review(db, project_id, user.id)
    except ERRORS as error:
        raise _error(error) from None
    response.headers.update(HEADERS)
    return result


@router.get("", response_model=list[ProjectLinkageInfo])
def list_reviews(project_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        result = project_linkage.list_reviews(db, project_id, user.id)
    except ERRORS as error:
        raise _error(error) from None
    response.headers.update(HEADERS)
    return result


@router.get("/{review_id}", response_model=ProjectLinkageInfo)
def get_review(
    project_id: UUID, review_id: UUID, user: CurrentUser, db: Database, response: Response
):
    try:
        result = project_linkage.get_review(db, project_id, review_id, user.id)
    except ERRORS as error:
        raise _error(error) from None
    response.headers.update(HEADERS)
    return result


@router.post(
    "/{review_id}/retry", response_model=ProjectLinkageInfo, dependencies=[Depends(require_csrf)]
)
def retry(project_id: UUID, review_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        result = project_linkage.retry_review(db, project_id, review_id, user.id)
    except ERRORS as error:
        raise _error(error) from None
    response.headers.update(HEADERS)
    return result


@router.post(
    "/{review_id}/findings/{finding_id}/request",
    response_model=RequestInfo,
    dependencies=[Depends(require_csrf)],
)
def convert_finding(
    project_id: UUID,
    review_id: UUID,
    finding_id: str,
    body: RequestCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
):
    try:
        result = project_linkage.convert_finding_to_request(
            db, project_id, review_id, finding_id, user.id, body
        )
    except ERRORS as error:
        raise _error(error) from None
    response.headers.update(HEADERS)
    return result
