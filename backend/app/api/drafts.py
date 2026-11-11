from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.deliveries import delivery_error, delivery_info
from app.db import get_session
from app.models.auth import User
from app.schemas.agent_drafts import AgentDraftView, DraftConfirm
from app.schemas.deliveries import DeliveryVersionInfo
from app.services import deliveries, draft_confirmation, projects
from app.storage import LocalStorage, UploadTooLargeError

router = APIRouter(prefix="/projects/{project_id}/drafts", tags=["drafts"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def draft_error(error: Exception) -> HTTPException:
    if isinstance(error, draft_confirmation.DraftAlreadyConfirmedError):
        result = HTTPException(409, "这份草稿已经确认提交，请查看对应正式版本")
    elif isinstance(error, draft_confirmation.DraftFileIntegrityError):
        result = HTTPException(409, "草稿文件校验失败，请重新上传草稿")
    elif isinstance(error, (draft_confirmation.DraftNotFoundError, FileNotFoundError)):
        result = HTTPException(404, "草稿不存在或文件不可用")
    else:
        result = delivery_error(error)
    result.headers = {**(result.headers or {}), "Cache-Control": "no-store"}
    return result


ERRORS = (
    projects.ProjectError,
    draft_confirmation.DraftNotFoundError,
    draft_confirmation.DraftAlreadyConfirmedError,
    draft_confirmation.DraftFileIntegrityError,
    deliveries.DeliveryNotFoundError,
    deliveries.DeliveryAssigneeRequiredError,
    deliveries.DeliveryValidationError,
    UploadTooLargeError,
    FileNotFoundError,
)


@router.get("", response_model=list[AgentDraftView])
def list_drafts(
    project_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
    module_id: UUID | None = None,
):
    try:
        result = draft_confirmation.list_drafts(db, project_id, user.id, module_id=module_id)
    except ERRORS as error:
        raise draft_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{draft_id}", response_model=AgentDraftView)
def get_draft(
    project_id: UUID, draft_id: UUID, user: CurrentUser, db: Database, response: Response
):
    try:
        result = draft_confirmation.get_draft(db, project_id, draft_id, user.id)
    except ERRORS as error:
        raise draft_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{draft_id}/download")
def download_draft(
    project_id: UUID, draft_id: UUID, user: CurrentUser, db: Database, request: Request
):
    try:
        path, filename = draft_confirmation.download_draft(
            db,
            project_id,
            draft_id,
            user.id,
            storage=LocalStorage(request.app.state.settings.storage_dir),
        )
    except ERRORS as error:
        raise draft_error(error) from None
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.post(
    "/{draft_id}/confirm",
    status_code=201,
    response_model=DeliveryVersionInfo,
    dependencies=[Depends(require_csrf)],
)
def confirm_draft(
    project_id: UUID,
    draft_id: UUID,
    body: DraftConfirm,
    user: CurrentUser,
    db: Database,
    request: Request,
    response: Response,
):
    settings = request.app.state.settings
    try:
        item = draft_confirmation.confirm_draft(
            db,
            project_id,
            draft_id,
            user.id,
            body,
            storage=LocalStorage(settings.storage_dir),
            max_bytes=settings.upload_max_bytes,
            review_return_limit=settings.review_return_limit,
        )
    except ERRORS as error:
        raise draft_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return delivery_info(item)
