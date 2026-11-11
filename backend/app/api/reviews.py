"""人类审核报告读取和意见标注；智能体保持只读接入。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.schemas.review_annotations import ReviewAnnotationCreate
from app.services import projects, review_reports

router = APIRouter(tags=["reviews"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def _review_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    if isinstance(error, review_reports.AnnotationPermissionError):
        return HTTPException(403, "只有提交人和同模块负责人可以标注审核意见")
    if isinstance(error, review_reports.AnnotationConflictError):
        return HTTPException(409, "审核意见或处理状态已变化，请刷新后重试")
    if isinstance(error, review_reports.AnnotationValidationError):
        return HTTPException(422, "请检查采纳标注和不采纳理由")
    return HTTPException(404, "交付或审核意见不存在")


_ERRORS = (
    projects.ProjectError,
    review_reports.ReportNotFoundError,
    review_reports.AnnotationPermissionError,
    review_reports.AnnotationConflictError,
    review_reports.AnnotationValidationError,
)


@router.get("/projects/{project_id}/deliveries/{delivery_id}/review", response_model=dict)
def get_review(
    project_id: UUID,
    delivery_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> dict:
    try:
        result = review_reports.get_review_report(db, project_id, delivery_id, user.id)
    except _ERRORS as error:
        raise _review_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/projects/{project_id}/deliveries/{delivery_id}/review/findings/{finding_id}/annotation",
    response_model=dict,
    dependencies=[Depends(require_csrf)],
)
def annotate_review(
    project_id: UUID,
    delivery_id: UUID,
    finding_id: UUID,
    body: ReviewAnnotationCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> dict:
    try:
        result = review_reports.annotate_finding(
            db,
            project_id,
            delivery_id,
            finding_id,
            user.id,
            handling=body.handling,
            reason=body.reason,
            expected_revision=body.expected_revision,
        )
    except _ERRORS as error:
        raise _review_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result
