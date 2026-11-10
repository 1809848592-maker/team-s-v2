from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.auth import require_csrf, require_user
from app.db import get_session
from app.models import Notification, User
from app.services import notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])
Database = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(require_user)]


class NotificationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    user_id: UUID
    kind: str
    message: str
    details: dict[str, Any]
    created_at: datetime
    read_at: datetime | None


@router.get("", response_model=list[NotificationInfo])
def list_notifications(
    user: CurrentUser, db: Database, response: Response, project_id: UUID | None = None
) -> list[Notification]:
    response.headers["Cache-Control"] = "no-store"
    return notifications.list_notifications(db, user.id, project_id)


@router.post(
    "/{notification_id}/read",
    response_model=NotificationInfo,
    dependencies=[Depends(require_csrf)],
)
def read_notification(
    notification_id: UUID, user: CurrentUser, db: Database, response: Response
) -> Notification:
    try:
        item = notifications.read_notification(db, notification_id, user.id)
    except notifications.NotificationNotFoundError:
        raise HTTPException(404, "通知不存在") from None
    response.headers["Cache-Control"] = "no-store"
    return item
