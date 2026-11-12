"""两类持久审核队列的调度，以及真实子进程的项目审核超时/停止边界。"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

from test_deliveries import ready_team
from test_project_linkage_service import start
from test_worker_runtime import process_exists, wait_for_marker

from app import config, db, worker
from app.models.project_linkage import ProjectLinkageReview
from app.services import project_linkage


def project_sleeping_handler(review):
    assert {field.name for field in fields(review)} == {
        "review_id",
        "project_id",
        "attempt",
        "claim_token",
    }
    Path(os.environ["TEAMS_M4_WORKER_MARKER"]).write_text(
        json.dumps({"pid": os.getpid(), "review_id": str(review.review_id)})
    )
    time.sleep(30)
    return {"result_id": "late_result_is_never_applied"}


def test_project_child_timeout_is_killed_and_task_is_incomplete(
    auth_environment, monkeypatch, tmp_path
):
    team = ready_team(auth_environment)
    identity = start(auth_environment, team)["id"]
    marker = tmp_path / "project-timeout.json"
    monkeypatch.setenv("TEAMS_M4_WORKER_MARKER", str(marker))
    assert worker.run_project_once(
        auth_environment.sessions,
        handler=project_sleeping_handler,
        timeout_seconds=3,
        max_attempts=1,
    )
    child = wait_for_marker(marker, timeout=0.1)
    assert child["review_id"] == identity and not process_exists(child["pid"])
    with auth_environment.sessions() as session:
        item = session.get(ProjectLinkageReview, UUID(identity))
        assert (item.status, item.error_code) == ("partial_failed", "timeout")
        assert item.result is None and item.claim_token is None and item.lease_until is None


def test_project_stop_preserves_committed_lease_for_recovery(
    auth_environment, monkeypatch, tmp_path
):
    team = ready_team(auth_environment)
    identity = start(auth_environment, team)["id"]
    marker = tmp_path / "project-stop.json"
    monkeypatch.setenv("TEAMS_M4_WORKER_MARKER", str(marker))
    stopped = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            worker.run_project_once,
            auth_environment.sessions,
            handler=project_sleeping_handler,
            timeout_seconds=20,
            stopped=stopped,
        )
        try:
            child = wait_for_marker(marker)
            with auth_environment.sessions() as session:
                item = session.get(ProjectLinkageReview, UUID(identity))
                token, expiry = item.claim_token, item.lease_until
                assert item.status == "running"
            stopped.set()
            assert future.result(timeout=8)
        finally:
            stopped.set()
    assert not process_exists(child["pid"])
    with auth_environment.sessions() as session:
        item = session.get(ProjectLinkageReview, UUID(identity))
        assert item.status == "running" and item.result is None
        assert (item.claim_token, item.lease_until) == (token, expiry)
        assert project_linkage.claim_next(session, now=expiry, retry_delay_seconds=0) is None
        session.commit()
    with auth_environment.sessions() as session:
        replacement = project_linkage.claim_next(session, now=expiry + timedelta(microseconds=1))
        session.commit()
        assert replacement.attempt == 2 and replacement.claim_token != token


def test_main_alternates_delivery_and_project_queues_without_starvation(monkeypatch, tmp_path):
    turns = []
    disposed = []

    def handler(name):
        def process(_sessions, **arguments):
            turns.append(name)
            if len(turns) == 4:
                arguments["stopped"].set()
            return True

        return process

    settings = config.Settings(_env_file=None, storage_dir=tmp_path / "storage")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "engine", SimpleNamespace(dispose=lambda: disposed.append(True)))
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker, "run_once", handler("delivery"))
    monkeypatch.setattr(worker, "run_project_once", handler("project"))
    monkeypatch.setattr(
        worker, "LocalReviewDebugStore", lambda *_: SimpleNamespace(purge_expired=lambda: None)
    )
    worker.main()
    assert turns == ["delivery", "project", "delivery", "project"] and disposed == [True]
