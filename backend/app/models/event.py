from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    String,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, ORMExecuteState, Session, mapped_column

from app.models.base import Base


class EventAppendOnlyError(ValueError):
    pass


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("actor_type IN ('human', 'agent')", name="ck_events_actor_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str] = mapped_column(String(128), index=True)
    represented_user_id: Mapped[UUID | None] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(80))
    object_id: Mapped[str] = mapped_column(String(256), index=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    plan_version_id: Mapped[UUID | None]
    spec_version_id: Mapped[UUID | None]
    task_id: Mapped[UUID | None] = mapped_column(index=True)


@event.listens_for(Event, "before_update")
@event.listens_for(Event, "before_delete")
def reject_event_mutation(mapper, connection, target) -> None:
    raise EventAppendOnlyError("事件日志只允许追加，不能修改或删除")


@event.listens_for(Session, "do_orm_execute")
def reject_bulk_event_mutation(state: ORMExecuteState) -> None:
    if state.is_update or state.is_delete:
        table = getattr(state.statement, "table", None)
        if table is not None and table.name == Event.__tablename__:
            raise EventAppendOnlyError("事件日志只允许追加，不能修改或删除")


@event.listens_for(Event.__table__, "after_create")
def protect_event_table(table, connection, **kwargs) -> None:
    if connection.dialect.name != "postgresql":
        return
    # create_all 的测试使用 schema_translate_map；函数与触发器留在同一隔离 schema。
    preparer = connection.dialect.identifier_preparer
    schema = connection.schema_for_object(table) or connection.dialect.default_schema_name
    prefix = f"{preparer.quote(schema)}." if schema else ""
    function = f"{prefix}reject_events_mutation"
    table_name = f"{prefix}{preparer.quote(table.name)}"
    connection.exec_driver_sql(
        f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'events are append-only' USING ERRCODE = '55000'; END; $$"
    )
    connection.exec_driver_sql(
        f"CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
    )


@event.listens_for(Event.__table__, "after_drop")
def drop_event_guard(table, connection, **kwargs) -> None:
    if connection.dialect.name == "postgresql":
        schema = connection.schema_for_object(table) or connection.dialect.default_schema_name
        prefix = f"{connection.dialect.identifier_preparer.quote(schema)}." if schema else ""
        connection.exec_driver_sql(f"DROP FUNCTION IF EXISTS {prefix}reject_events_mutation()")
