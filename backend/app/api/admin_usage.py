"""平台管理员用量和额度 API；人类会话与智能体通道保持隔离。"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.db import get_session
from app.models import User
from app.schemas.admin_usage import AdminProjectInfo, ProjectQuotaUpdate, UsageReport
from app.services import admin_usage

router = APIRouter(prefix="/admin", tags=["admin-usage"])
Database = Annotated[Session, Depends(get_session)]


def require_admin(request: Request, db: Database) -> User:
    try:
        user = require_user(request, db)
        admin_usage.require_admin(user.id, request.app.state.settings)
    except HTTPException as error:
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        raise
    except admin_usage.AdminRequiredError:
        raise HTTPException(
            403, "只有平台管理员可以访问此页面", headers={"Cache-Control": "no-store"}
        ) from None
    return user


Administrator = Annotated[User, Depends(require_admin)]


def admin_csrf(request: Request) -> None:
    try:
        require_csrf(request)
    except HTTPException as error:
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        raise


def _error(error: Exception) -> HTTPException:
    if isinstance(error, admin_usage.AdminProjectNotFoundError):
        return HTTPException(404, "项目不存在", headers={"Cache-Control": "no-store"})
    return HTTPException(422, str(error), headers={"Cache-Control": "no-store"})


@router.get("/usage", response_model=UsageReport)
def usage(
    user: Administrator,
    db: Database,
    request: Request,
    response: Response,
    start: date,
    end: date,
    project_id: UUID | None = None,
) -> UsageReport:
    try:
        result = admin_usage.usage_report(
            db, user.id, request.app.state.settings, start, end, project_id
        )
    except (admin_usage.UsageRangeError, admin_usage.AdminProjectNotFoundError) as error:
        raise _error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/usage.csv")
def export_usage(
    user: Administrator,
    db: Database,
    request: Request,
    start: date,
    end: date,
    project_id: UUID | None = None,
) -> Response:
    try:
        content = admin_usage.usage_csv(
            db, user.id, request.app.state.settings, start, end, project_id
        )
    except (admin_usage.UsageRangeError, admin_usage.AdminProjectNotFoundError) as error:
        raise _error(error) from None
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="teams-usage-{start}-{end}.csv"',
        },
    )


@router.get("/projects", response_model=list[AdminProjectInfo])
def projects(user: Administrator, db: Database, request: Request, response: Response):
    result = admin_usage.list_projects(db, user.id, request.app.state.settings)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.put(
    "/projects/{project_id}/quota",
    response_model=AdminProjectInfo,
    dependencies=[Depends(admin_csrf)],
)
def update_quota(
    project_id: UUID,
    body: ProjectQuotaUpdate,
    user: Administrator,
    db: Database,
    request: Request,
    response: Response,
):
    try:
        result = admin_usage.update_project_quota(
            db, user.id, request.app.state.settings, project_id, body
        )
    except admin_usage.AdminProjectNotFoundError as error:
        raise _error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result
