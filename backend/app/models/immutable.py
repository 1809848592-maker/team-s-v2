"""发布快照的 ORM 与 PostgreSQL 不可变守卫，供方案及规范复用。"""

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session

from app.models.base import Base

_SNAPSHOT_TABLES: set[str] = set()


class SnapshotImmutableError(ValueError):
    pass


def _reject_mutation(mapper, connection, target) -> None:
    raise SnapshotImmutableError("已发布的版本不可修改或删除，请发布新版本")


@event.listens_for(Session, "do_orm_execute")
def _reject_bulk_mutation(state: ORMExecuteState) -> None:
    if state.is_update or state.is_delete:
        table = getattr(state.statement, "table", None)
        if table is not None and table.name in _SNAPSHOT_TABLES:
            raise SnapshotImmutableError("已发布的版本不可修改或删除，请发布新版本")


def _qualified_names(table, connection) -> tuple[str, str]:
    preparer = connection.dialect.identifier_preparer
    schema = connection.schema_for_object(table) or connection.dialect.default_schema_name
    prefix = f"{preparer.quote(schema)}." if schema else ""
    return (
        f"{prefix}{preparer.quote(table.name)}",
        f"{prefix}{preparer.quote(f'reject_{table.name}_mutation')}",
    )


def _protect_table(table, connection, **kwargs) -> None:
    if connection.dialect.name == "postgresql":
        table_name, function = _qualified_names(table, connection)
        trigger = connection.dialect.identifier_preparer.quote(f"{table.name}_immutable")
        connection.exec_driver_sql(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
            "USING ERRCODE = '55000'; END; $$"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {function}()"
        )


def _drop_guard(table, connection, **kwargs) -> None:
    if connection.dialect.name == "postgresql":
        _, function = _qualified_names(table, connection)
        connection.exec_driver_sql(f"DROP FUNCTION IF EXISTS {function}()")


def protect_snapshot(model: type[Base]) -> None:
    _SNAPSHOT_TABLES.add(model.__tablename__)
    event.listen(model, "before_update", _reject_mutation)
    event.listen(model, "before_delete", _reject_mutation)
    event.listen(model.__table__, "after_create", _protect_table)
    event.listen(model.__table__, "after_drop", _drop_guard)
