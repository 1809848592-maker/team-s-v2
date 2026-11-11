"""负责人终审及规范不适用裁定，始终通过人类身份通道。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.schemas.final_reviews import FinalReviewCreate
from app.services import final_review, projects

router = APIRouter(tags=["final-review"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def _final_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.ProjectError):
        mapped = project_error(error)
        mapped.headers = {**(mapped.headers or {}), "Cache-Control": "no-store"}
        return mapped
    if isinstance(error, final_review.FinalReviewPermissionError):
        return HTTPException(
            403,
            "只有符合自审规则的项目负责人可以终审或裁定规范不适用",
            headers={"Cache-Control": "no-store"},
        )
    if isinstance(error, final_review.FinalReviewConflictError):
        return HTTPException(
            409,
            "审核状态或处理记录已变化，请刷新后重试",
            headers={"Cache-Control": "no-store"},
        )
    if isinstance(error, final_review.FinalReviewValidationError):
        return HTTPException(
            422,
            "请检查终审决定、退回意见和规范不适用裁定",
            headers={"Cache-Control": "no-store"},
        )
    return HTTPException(404, "交付或终审报告不存在", headers={"Cache-Control": "no-store"})


_ERRORS = (
    projects.ProjectError,
    final_review.FinalReviewNotFoundError,
    final_review.FinalReviewPermissionError,
    final_review.FinalReviewConflictError,
    final_review.FinalReviewValidationError,
)


@router.get("/projects/{project_id}/deliveries/{delivery_id}/review/final", response_model=dict)
def get_final_review(
    project_id: UUID,
    delivery_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> dict:
    try:
        result = final_review.get_final_review(db, project_id, delivery_id, user.id)
    except _ERRORS as error:
        raise _final_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post(
    "/projects/{project_id}/deliveries/{delivery_id}/review/final",
    response_model=dict,
    dependencies=[Depends(require_csrf)],
)
def decide_final_review(
    project_id: UUID,
    delivery_id: UUID,
    body: FinalReviewCreate,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> dict:
    try:
        result = final_review.decide_final_review(db, project_id, delivery_id, user.id, body)
    except _ERRORS as error:
        raise _final_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result
