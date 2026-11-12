from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.services import model_disclosure, projects

router = APIRouter(tags=["model disclosure"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


class ConsentAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disclosure_version: str = Field(min_length=1, max_length=80)


@router.get("/model-disclosure")
def current_notice(user: CurrentUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return model_disclosure.notice()


@router.get("/projects/{project_id}/model-disclosure")
def project_notice(project_id: UUID, user: CurrentUser, db: Database, response: Response):
    try:
        result = model_disclosure.get_project_disclosure(db, project_id, user.id)
    except projects.ProjectError as error:
        response_error = project_error(error)
        response_error.headers = {"Cache-Control": "no-store"}
        raise response_error from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/projects/{project_id}/model-consent", dependencies=[Depends(require_csrf)])
def consent(
    project_id: UUID, body: ConsentAccept, user: CurrentUser, db: Database, response: Response
):
    try:
        result = model_disclosure.accept_project_consent(
            db, project_id, user.id, body.disclosure_version
        )
    except projects.LeaderRequiredError:
        raise HTTPException(
            403, "只有项目负责人可以确认数据外发知情同意", headers={"Cache-Control": "no-store"}
        ) from None
    except projects.ProjectError as error:
        response_error = project_error(error)
        response_error.headers = {"Cache-Control": "no-store"}
        raise response_error from None
    except model_disclosure.DisclosureVersionError as error:
        raise HTTPException(409, str(error), headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    return result
