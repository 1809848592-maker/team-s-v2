from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.presets.catalog import preset_catalog
from app.services import projects, resources
from app.storage import LocalStorage, UploadTooLargeError

router = APIRouter(tags=["resources"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


class ResourceInfo(BaseModel):
    id: UUID
    filename: str
    size: int
    sha256: str
    builtin: bool
    download_url: str


class PresetInfo(BaseModel):
    key: str
    name: str
    items: list[dict[str, Any]]


class PresetCatalog(BaseModel):
    presets: list[PresetInfo]
    templates: list[ResourceInfo]


def resource_info(item: resources.ResolvedResource, project_id: UUID) -> ResourceInfo:
    return ResourceInfo(
        id=item.id,
        filename=item.filename,
        size=item.size,
        sha256=item.sha256,
        builtin=item.builtin,
        download_url=f"/api/projects/{project_id}/resources/{item.id}/download",
    )


def resource_error(error: Exception) -> HTTPException:
    if isinstance(error, projects.LeaderRequiredError):
        return HTTPException(403, "只有项目负责人可以上传模板")
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    if isinstance(error, UploadTooLargeError):
        return HTTPException(413, "文件超过上传大小上限")
    if isinstance(error, resources.ResourceValidationError):
        return HTTPException(422, str(error))
    return HTTPException(404, "资源不存在或文件不可用")


@router.get("/projects/{project_id}/spec/presets", response_model=PresetCatalog)
def get_presets(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> PresetCatalog:
    try:
        projects.require_member(db, project_id, user.id)
    except projects.ProjectError as error:
        raise resource_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return PresetCatalog(
        presets=[PresetInfo(**item) for item in preset_catalog()],
        templates=[resource_info(item, project_id) for item in resources.builtin_resources()],
    )


@router.get("/projects/{project_id}/resources", response_model=list[ResourceInfo])
def list_resources(
    project_id: UUID, user: CurrentUser, db: Database, response: Response, request: Request
) -> list[ResourceInfo]:
    try:
        items = resources.list_resources(
            db, project_id, user.id, storage=LocalStorage(request.app.state.settings.storage_dir)
        )
    except projects.ProjectError as error:
        raise resource_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return [resource_info(item, project_id) for item in items]


@router.post(
    "/projects/{project_id}/resources",
    status_code=201,
    response_model=ResourceInfo,
    dependencies=[Depends(require_csrf)],
)
def upload_resource(
    project_id: UUID,
    user: CurrentUser,
    db: Database,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
) -> ResourceInfo:
    settings = request.app.state.settings
    try:
        item = resources.upload_resource(
            db,
            project_id,
            user.id,
            file.filename or "",
            file.file,
            storage=LocalStorage(settings.storage_dir),
            max_bytes=settings.upload_max_bytes,
        )
    except (
        projects.ProjectError,
        resources.ResourceValidationError,
        UploadTooLargeError,
    ) as error:
        raise resource_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return resource_info(item, project_id)


@router.get("/projects/{project_id}/resources/{resource_id}/download")
def download_resource(
    project_id: UUID, resource_id: UUID, user: CurrentUser, db: Database, request: Request
) -> FileResponse:
    try:
        item = resources.resolve_resource(
            db,
            project_id,
            user.id,
            resource_id,
            storage=LocalStorage(request.app.state.settings.storage_dir),
        )
        if not item.path.is_file():
            raise resources.ResourceNotFoundError
    except (projects.ProjectError, resources.ResourceNotFoundError) as error:
        raise resource_error(error) from None
    return FileResponse(
        item.path,
        filename=item.filename,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )
