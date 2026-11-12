from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.models.delivery import DeliveryVersion, SharedMaterial
from app.schemas.deliveries import DeliverySubmit, DeliveryVersionInfo, SharedMaterialInfo
from app.schemas.version_binding import ResubmissionRequest, VersionBindingInfo
from app.services import deliveries, model_disclosure, projects, version_binding
from app.storage import LocalStorage, UploadTooLargeError

router = APIRouter(tags=["deliveries"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


def delivery_info(
    item: DeliveryVersion, binding: VersionBindingInfo | None = None
) -> DeliveryVersionInfo:
    return DeliveryVersionInfo.model_validate(item).model_copy(
        update={
            "version_binding": binding,
            "download_url": (
                f"/api/projects/{item.project_id}/modules/{item.module_id}"
                f"/deliveries/{item.id}/download"
            ),
        }
    )


def delivery_infos(
    db: Session, items: list[DeliveryVersion], user_id: UUID
) -> list[DeliveryVersionInfo]:
    if not items:
        return []
    member = projects.require_member(db, items[0].project_id, user_id)
    bindings = version_binding.binding_info(db, items, leader=member.role == "leader")
    return [delivery_info(item, bindings[item.id]) for item in items]


def material_info(item: SharedMaterial) -> SharedMaterialInfo:
    return SharedMaterialInfo.model_validate(item).model_copy(
        update={"download_url": f"/api/projects/{item.project_id}/materials/{item.id}/download"}
    )


def delivery_error(error: Exception) -> HTTPException:
    if isinstance(error, model_disclosure.DisclosureVersionError):
        return HTTPException(409, str(error), headers={"Cache-Control": "no-store"})
    if isinstance(error, projects.ProjectError):
        return project_error(error)
    if isinstance(error, deliveries.DeliveryAssigneeRequiredError):
        return HTTPException(403, "只有该模块的负责人可以提交正式交付")
    if isinstance(error, deliveries.DeliveryValidationError):
        return HTTPException(422, str(error))
    if isinstance(error, UploadTooLargeError):
        return HTTPException(413, "文件超过上传大小上限")
    return HTTPException(404, "交付或共享材料不存在，或文件不可用")


@router.post(
    "/projects/{project_id}/modules/{module_id}/deliveries",
    status_code=201,
    response_model=DeliveryVersionInfo,
    dependencies=[Depends(require_csrf)],
)
def submit_delivery(
    project_id: UUID,
    module_id: UUID,
    user: CurrentUser,
    db: Database,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    ai_participation: Annotated[Literal["none", "assisted", "primary"], Form()],
    ai_note: Annotated[str, Form(max_length=2000)] = "",
    handoff_note: Annotated[str, Form(max_length=10000)] = "",
    disclosure_version: Annotated[str | None, Form(max_length=80)] = None,
) -> DeliveryVersionInfo:
    settings = request.app.state.settings
    try:
        submission = DeliverySubmit(
            ai_participation=ai_participation,
            ai_note=ai_note,
            handoff_note=handoff_note,
            disclosure_version=disclosure_version,
        )
    except ValidationError:
        raise HTTPException(422, "请检查 AI 参与声明和交接说明") from None
    try:
        item = deliveries.submit_delivery(
            db,
            project_id,
            module_id,
            user.id,
            file.filename or "",
            file.file,
            submission,
            storage=LocalStorage(settings.storage_dir),
            max_bytes=settings.upload_max_bytes,
            review_return_limit=settings.review_return_limit,
        )
    except (
        projects.ProjectError,
        deliveries.DeliveryNotFoundError,
        deliveries.DeliveryAssigneeRequiredError,
        deliveries.DeliveryValidationError,
        UploadTooLargeError,
        model_disclosure.DisclosureVersionError,
    ) as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return delivery_infos(db, [item], user.id)[0]


@router.get(
    "/projects/{project_id}/modules/{module_id}/deliveries",
    response_model=list[DeliveryVersionInfo],
)
def module_deliveries(
    project_id: UUID, module_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[DeliveryVersionInfo]:
    try:
        items = deliveries.list_deliveries(db, project_id, user.id, module_id=module_id)
    except (projects.ProjectError, deliveries.DeliveryNotFoundError) as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return delivery_infos(db, items, user.id)


@router.get("/projects/{project_id}/deliveries", response_model=list[DeliveryVersionInfo])
def project_deliveries(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[DeliveryVersionInfo]:
    try:
        items = deliveries.list_deliveries(db, project_id, user.id)
    except projects.ProjectError as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return delivery_infos(db, items, user.id)


@router.post(
    "/projects/{project_id}/deliveries/{delivery_id}/resubmission-requests",
    response_model=DeliveryVersionInfo,
    dependencies=[Depends(require_csrf)],
)
def request_resubmission(
    project_id: UUID,
    delivery_id: UUID,
    body: ResubmissionRequest,
    user: CurrentUser,
    db: Database,
    response: Response,
) -> DeliveryVersionInfo:
    try:
        item = version_binding.request_resubmission(db, project_id, delivery_id, user.id, body)
    except version_binding.ResubmissionConflictError as error:
        raise HTTPException(409, str(error), headers={"Cache-Control": "no-store"}) from None
    except (projects.ProjectError, deliveries.DeliveryNotFoundError) as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return delivery_infos(db, [item], user.id)[0]


def _file_response(item: DeliveryVersion | SharedMaterial, request: Request) -> FileResponse:
    path = LocalStorage(request.app.state.settings.storage_dir).path(item.sha256)
    if not path.is_file():
        raise HTTPException(404, "交付或共享材料不存在，或文件不可用")
    return FileResponse(
        path,
        filename=item.filename,
        media_type="application/octet-stream",
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
    )


@router.get("/projects/{project_id}/modules/{module_id}/deliveries/{delivery_id}/download")
def download_delivery(
    project_id: UUID,
    module_id: UUID,
    delivery_id: UUID,
    user: CurrentUser,
    db: Database,
    request: Request,
) -> FileResponse:
    try:
        item = deliveries.get_delivery(db, project_id, module_id, user.id, delivery_id)
    except (projects.ProjectError, deliveries.DeliveryNotFoundError) as error:
        raise delivery_error(error) from None
    return _file_response(item, request)


@router.post(
    "/projects/{project_id}/materials",
    status_code=201,
    response_model=SharedMaterialInfo,
    dependencies=[Depends(require_csrf)],
)
def upload_material(
    project_id: UUID,
    user: CurrentUser,
    db: Database,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
) -> SharedMaterialInfo:
    settings = request.app.state.settings
    try:
        item = deliveries.upload_material(
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
        deliveries.DeliveryValidationError,
        UploadTooLargeError,
    ) as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return material_info(item)


@router.get("/projects/{project_id}/materials", response_model=list[SharedMaterialInfo])
def project_materials(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[SharedMaterialInfo]:
    try:
        items = deliveries.list_materials(db, project_id, user.id)
    except projects.ProjectError as error:
        raise delivery_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return [material_info(item) for item in items]


@router.get("/projects/{project_id}/materials/{material_id}/download")
def download_material(
    project_id: UUID, material_id: UUID, user: CurrentUser, db: Database, request: Request
) -> FileResponse:
    try:
        item = deliveries.get_material(db, project_id, user.id, material_id)
    except (projects.ProjectError, deliveries.MaterialNotFoundError) as error:
        raise delivery_error(error) from None
    return _file_response(item, request)
