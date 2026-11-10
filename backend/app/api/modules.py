from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.auth import require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.schemas.plans import ModuleInfo
from app.services import modules, projects

router = APIRouter(prefix="/projects/{project_id}/modules", tags=["modules"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


@router.get("", response_model=list[ModuleInfo])
def list_modules(
    project_id: UUID, user: CurrentUser, db: Database, response: Response
) -> list[ModuleInfo]:
    try:
        items = modules.list_modules(db, project_id, user.id)
    except projects.ProjectError as error:
        raise project_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return items


@router.get("/{module_id}", response_model=ModuleInfo)
def get_module(
    project_id: UUID, module_id: UUID, user: CurrentUser, db: Database, response: Response
) -> ModuleInfo:
    try:
        item = modules.get_module(db, project_id, module_id, user.id)
    except modules.ModuleNotFoundError:
        raise HTTPException(404, "该方案中没有此分工模块") from None
    except projects.ProjectError as error:
        raise project_error(error) from None
    response.headers["Cache-Control"] = "no-store"
    return item
