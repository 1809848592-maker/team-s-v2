"""Initialise only the explicitly selected demo database and paired storage volume."""

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.demo import DemoError, create_dataset, validate_demo_url
from app.models import AgentDraft, Base, DeliveryVersion, Event, Resource, SharedMaterial
from app.storage import LocalStorage


def _migrate(connection):
    from alembic.config import Config

    from alembic import command

    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def _write_state(root, state):
    descriptor, name = tempfile.mkstemp(prefix=".demo-state-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as target:
            json.dump(state, target, ensure_ascii=False, sort_keys=True, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, root / ".demo-state.json")
    finally:
        Path(name).unlink(missing_ok=True)


def _existing_manifest(sessions, tables):
    if "events" not in tables:
        return None
    with sessions() as db:
        markers = list(
            db.scalars(select(Event).where(Event.event_type == "demo_seeded").order_by(Event.id))
        )
        if len(markers) > 1:
            raise DemoError("演示完成标记不唯一，拒绝继续。")
        return markers[0].details if markers else None


def _verify_files(sessions, root):
    storage = LocalStorage(root / "content")
    if not storage.root.is_dir() or storage.root.is_symlink():
        raise DemoError("演示文件卷缺失或不匹配。")
    with sessions() as db:
        for model in (DeliveryVersion, AgentDraft, SharedMaterial, Resource):
            for sha256, size in db.execute(select(model.sha256, model.size).distinct()):
                path = storage.path(sha256)
                if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                    raise DemoError("演示文件缺失或损坏，不把初始化记为完成。")
                with path.open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                        raise DemoError("演示文件哈希不符，不把初始化记为完成。")


def bootstrap_database(engine, root, *, migrate=_migrate):
    """The CLI validates the target URL; supplied engines/migration seams support isolated tests."""
    root = Path(root)
    if not root.is_absolute() or root == Path(root.anchor) or root.is_symlink():
        raise DemoError("必须提供独立、绝对路径的演示文件卷。")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".demo.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DemoError("另一个演示初始化正在运行。") from None
        state_path = root / ".demo-state.json"
        try:
            state = json.loads(state_path.read_text()) if state_path.exists() else None
            if state is not None:
                UUID(state["identity"])
                if state["format"] != 1 or type(state["complete"]) is not bool:
                    raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise DemoError("演示文件卷标记无效，拒绝修改。") from None
        staging = None
        try:
            with engine.begin() as connection:
                # Coordinate duplicate seeders even when accidentally given different file roots.
                if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(441705005)")):
                    raise DemoError("另一个进程正在初始化此演示数据库。")
                schema = (
                    connection.get_execution_options().get("schema_translate_map", {}).get(None)
                )
                tables = set(inspect(connection).get_table_names(schema=schema))
                if tables - (set(Base.metadata.tables) | {"alembic_version"}):
                    raise DemoError("演示库包含无关表，拒绝修改。")
                sessions = sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                )
                manifest = _existing_manifest(sessions, tables)
                if manifest is not None:
                    if state is None or manifest.get("storage_identity") != state["identity"]:
                        raise DemoError("数据库与演示文件卷不配对，拒绝继续。")
                    _verify_files(sessions, root)
                else:
                    if state is not None or any(
                        path.name != ".demo.lock" for path in root.iterdir()
                    ):
                        raise DemoError(
                            "演示文件卷已有数据或上次初始化未完成，请保留现场并换用全新演示环境。"
                        )
                    # Check populated tables before migrations or storing any pending marker.
                    with sessions() as db:
                        if any(
                            db.execute(select(table).limit(1)).first()
                            for table in Base.metadata.sorted_tables
                            if table.name in tables
                        ):
                            raise DemoError("演示库已有非演示数据，拒绝修改。")
                    state = {"format": 1, "identity": str(uuid4()), "complete": False}
                    _write_state(root, state)
                    staging = Path(tempfile.mkdtemp(prefix=".demo-seed-", dir=root))
                    migrate(connection)
                    dataset = create_dataset(
                        sessions, LocalStorage(staging), storage_identity=state["identity"]
                    )
                    manifest = dataset.to_manifest()
                    staging.rename(root / "content")
            # A pending marker with a committed matching DB can be completed on the next run.
            if not state["complete"]:
                _write_state(root, {**state, "complete": True})
            return manifest
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging)


def main():
    os.umask(0o077)
    try:
        raw_url = os.environ.get("DEMO_DATABASE_URL")
        storage = os.environ.get("DEMO_STORAGE_DIR")
        if not raw_url or not storage:
            raise DemoError(
                "必须显式提供DEMO_DATABASE_URL和DEMO_STORAGE_DIR；不读取.env或默认数据库。"
            )
        url = validate_demo_url(raw_url)
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
        try:
            result = bootstrap_database(engine, Path(storage))
        finally:
            engine.dispose()
        print(json.dumps(result, ensure_ascii=False))
    except DemoError as error:
        print(f"演示初始化失败：{error}", file=sys.stderr)
        return 1
    except Exception:
        # Database exceptions can contain credentials or bound values; never print their repr.
        print("演示初始化失败；未标记完成，请检查独立演示库及文件卷状态。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
