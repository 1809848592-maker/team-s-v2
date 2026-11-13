"""CLI-only bootstrap ownership, transaction, idempotence and storage pairing."""

import json

import pytest
from sqlalchemy import select

from app.demo import DemoError
from app.demo_bootstrap import bootstrap_database
from app.models import Base, Event, Project


def database(environment):
    return environment.sessions.kw["bind"]


def snapshot(environment):
    with environment.sessions() as db:
        return {
            table.name: list(db.execute(select(table).order_by(*table.primary_key.columns)))
            for table in Base.metadata.sorted_tables
        }


def bootstrap(environment, path, **options):
    return bootstrap_database(
        database(environment), path, migrate=lambda connection: None, **options
    )


def test_completed_rerun_preserves_all_rows_files_and_user_edits(auth_environment, tmp_path):
    root = tmp_path / "isolated-demo"
    manifest = bootstrap(auth_environment, root)
    with auth_environment.sessions() as db:
        project = db.get(Project, manifest["project_id"])
        project.name = "老师现场修改后的演示题目"
        db.commit()
    before = snapshot(auth_environment)
    files = {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    repeated = bootstrap(auth_environment, root)
    assert repeated == manifest
    assert snapshot(auth_environment) == before
    assert {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    } == files
    assert (root / "content").is_dir()


def test_unmarked_nonempty_store_is_refused_without_touching_database(auth_environment, tmp_path):
    root = tmp_path / "do-not-touch"
    root.mkdir()
    keep = root / "existing-file"
    keep.write_bytes(b"original")
    before = snapshot(auth_environment)
    with pytest.raises(DemoError):
        bootstrap(auth_environment, root)
    assert keep.read_bytes() == b"original"
    assert snapshot(auth_environment) == before


def test_completed_database_with_other_storage_is_refused(auth_environment, tmp_path):
    bootstrap(auth_environment, tmp_path / "first")
    before = snapshot(auth_environment)
    with pytest.raises(DemoError):
        bootstrap(auth_environment, tmp_path / "other")
    assert snapshot(auth_environment) == before


def test_service_commits_are_rolled_back_when_initialization_fails(
    auth_environment, tmp_path, monkeypatch
):
    from app import demo_bootstrap

    real = demo_bootstrap.create_dataset
    before = snapshot(auth_environment)

    def fail_after_commits(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("fixed interruption")

    monkeypatch.setattr(demo_bootstrap, "create_dataset", fail_after_commits)
    with pytest.raises(RuntimeError, match="fixed interruption"):
        bootstrap(auth_environment, tmp_path / "failed")
    assert snapshot(auth_environment) == before
    with pytest.raises(DemoError):
        bootstrap(auth_environment, tmp_path / "failed")


def test_missing_file_is_not_reported_as_complete(auth_environment, tmp_path):
    root = tmp_path / "demo"
    bootstrap(auth_environment, root)
    content = next(path for path in (root / "content").rglob("*") if path.is_file())
    content.unlink()
    before = snapshot(auth_environment)
    with pytest.raises(DemoError):
        bootstrap(auth_environment, root)
    assert snapshot(auth_environment) == before


def test_pending_storage_marker_recovers_only_after_matching_database_commit(
    auth_environment, tmp_path
):
    root = tmp_path / "demo"
    expected = bootstrap(auth_environment, root)
    state_path = root / ".demo-state.json"
    state = json.loads(state_path.read_text())
    state["complete"] = False
    state_path.write_text(json.dumps(state))
    assert bootstrap(auth_environment, root) == expected
    assert json.loads(state_path.read_text())["complete"] is True
    with auth_environment.sessions() as db:
        assert len(list(db.scalars(select(Event).where(Event.event_type == "demo_seeded")))) == 1


def test_held_storage_lock_rejects_concurrent_initialization(auth_environment, tmp_path):
    import fcntl

    root = tmp_path / "locked"
    root.mkdir()
    with (root / ".demo.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DemoError, match="另一个"):
            bootstrap(auth_environment, root)
    with auth_environment.sessions() as db:
        assert list(db.scalars(select(Project))) == []
