"""事务内的只追加日志；调用方仅提供必要元数据，不记录凭据或交付物全文。"""

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Event

EventActorKind = Literal["human", "agent", "platform", "participant"]
# Reserved identities are not user-created agents (whose IDs are UUIDs). Historical
# append-only events can lack execution_origin; identify them without rewriting logs.
PLATFORM_WORKER_ORIGINS = {
    "platform-review-worker": "platform_worker",
    "platform-project-linkage-worker": "platform_project_linkage",
    "platform-plan-self-checker": "platform_plan_self_check",
    "platform-spec-drafter": "platform_spec_draft",
}

EVENT_FIELDS = (
    "id",
    "occurred_at",
    "project_id",
    "actor_type",
    "actor_id",
    "represented_user_id",
    "event_type",
    "object_type",
    "object_id",
    "details",
    "plan_version_id",
    "spec_version_id",
    "task_id",
)
_SECRET_FIELDS = {
    "password",
    "password_hash",
    "token",
    "raw_token",
    "api_key",
    "authorization",
    "file_content",
    "file_bytes",
    "full_text",
}


def _check_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _SECRET_FIELDS:
                raise ValueError("事件日志不能包含密码、令牌或文件全文")
            _check_metadata(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_metadata(item)


def record_event(
    db: Session,
    *,
    actor_id: UUID | str,
    event_type: str,
    object_type: str,
    object_id: UUID | str,
    project_id: UUID | None = None,
    actor_type: Literal["human", "agent"] = "human",
    represented_user_id: UUID | None = None,
    details: dict[str, Any] | None = None,
    plan_version_id: UUID | None = None,
    spec_version_id: UUID | None = None,
    task_id: UUID | None = None,
) -> Event:
    metadata = details if details is not None else {}
    _check_metadata(metadata)
    # 校验 JSON 可序列化并复制，避免调用方之后修改同一字典影响待提交内容。
    metadata = json.loads(json.dumps(metadata, ensure_ascii=False))
    if actor_type == "agent" and represented_user_id is None:
        origin = PLATFORM_WORKER_ORIGINS.get(str(actor_id))
        if origin is not None:
            metadata.setdefault("execution_origin", origin)
    item = Event(
        occurred_at=datetime.now(UTC),
        project_id=project_id,
        actor_type=actor_type,
        actor_id=str(actor_id),
        represented_user_id=represented_user_id,
        event_type=event_type,
        object_type=object_type,
        object_id=str(object_id),
        details=metadata,
        plan_version_id=plan_version_id,
        spec_version_id=spec_version_id,
        task_id=task_id,
    )
    db.add(item)
    db.flush()
    return item


def list_events(
    db: Session,
    project_id: UUID,
    *,
    task_id: UUID | None = None,
    object_id: str | None = None,
    actor_id: str | None = None,
    actor_kind: EventActorKind | None = None,
    limit: int | None = 100,
    before_id: int | None = None,
) -> list[Event]:
    query = select(Event).where(Event.project_id == project_id)
    platform_actor = and_(
        Event.actor_type == "agent",
        Event.actor_id.in_(PLATFORM_WORKER_ORIGINS),
        Event.represented_user_id.is_(None),
    )
    if actor_kind == "platform":
        query = query.where(platform_actor)
    elif actor_kind == "participant":
        query = query.where(~platform_actor)
    elif actor_kind == "human":
        query = query.where(Event.actor_type == "human")
    elif actor_kind == "agent":
        query = query.where(Event.actor_type == "agent", ~platform_actor)
    for column, value in (
        (Event.task_id, task_id),
        (Event.object_id, object_id),
        (Event.actor_id, actor_id),
    ):
        if value is not None:
            query = query.where(column == value)
    if before_id is not None:
        query = query.where(Event.id < before_id)
    if limit is not None:
        # 最近一页按时间正序呈现；before_id 可继续查看更早的事件。
        items = list(db.scalars(query.order_by(Event.id.desc()).limit(limit)))
        return list(reversed(items))
    return list(db.scalars(query.order_by(Event.id)))


def event_dict(item: Event) -> dict[str, Any]:
    result = {}
    for key in EVENT_FIELDS:
        value = getattr(item, key)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        result[key] = value
    return result


def _csv_cell(value: Any) -> str:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = "" if value is None else str(value)
    if text[:1] in ("\t", "\r", "\n") or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def events_csv(items: list[Event]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(EVENT_FIELDS)
    for item in items:
        values = event_dict(item)
        writer.writerow(_csv_cell(values[key]) for key in EVENT_FIELDS)
    return output.getvalue()
