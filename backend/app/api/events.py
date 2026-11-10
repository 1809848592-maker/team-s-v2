import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import events
from app.api.auth import require_user
from app.api.projects import project_error
from app.db import get_session
from app.models import User
from app.services import projects

router = APIRouter(prefix="/projects/{project_id}/events", tags=["events"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


class EventInfo(BaseModel):
    id: int
    occurred_at: datetime
    project_id: UUID | None
    actor_type: Literal["human", "agent"]
    actor_id: str
    represented_user_id: UUID | None
    event_type: str
    object_type: str
    object_id: str
    details: dict[str, Any]
    plan_version_id: UUID | None
    spec_version_id: UUID | None
    task_id: UUID | None


def require_access(db: Session, project_id: UUID, user_id: UUID, *, leader: bool = False) -> None:
    try:
        projects.require_member(db, project_id, user_id, leader=leader)
    except projects.LeaderRequiredError:
        raise HTTPException(403, "只有项目负责人可以导出事件日志") from None
    except projects.ProjectError as error:
        raise project_error(error) from None


@router.get("", response_model=list[EventInfo])
def project_events(
    project_id: UUID,
    user: CurrentUser,
    db: Database,
    response: Response,
    task_id: UUID | None = None,
    object_id: Annotated[str | None, Query(max_length=256)] = None,
    actor_id: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[dict]:
    require_access(db, project_id, user.id)
    response.headers["Cache-Control"] = "no-store"
    return [
        events.event_dict(item)
        for item in events.list_events(
            db,
            project_id,
            task_id=task_id,
            object_id=object_id,
            actor_id=actor_id,
            limit=limit,
            before_id=before_id,
        )
    ]


@router.get("/export")
def export_events(
    project_id: UUID,
    user: CurrentUser,
    db: Database,
    format: Literal["json", "csv"] = "json",
) -> Response:
    require_access(db, project_id, user.id, leader=True)
    items = events.list_events(db, project_id, limit=None)
    if format == "csv":
        content = events.events_csv(items)
        media_type = "text/csv"
    else:
        content = json.dumps([events.event_dict(item) for item in items], ensure_ascii=False)
        media_type = "application/json"
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="project-{project_id}-events.{format}"',
            "Cache-Control": "no-store",
        },
    )
