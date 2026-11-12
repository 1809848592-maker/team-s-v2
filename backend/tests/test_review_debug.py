"""14.9失败现场：本机留存、30天期限、管理员权限及模型替身边界。"""

import json
import stat
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
from test_agents import bearer, register_agent
from test_auth import login, register
from test_deliveries import ready_team
from test_llm_transport import PRIVATE_KEY, client, complete, response_body
from test_llm_transport import ledger as ledger

from app import worker
from app.config import Settings
from app.review.debug_records import LocalReviewDebugStore, ReviewDebugError
from app.review.factory import build_llm_client
from app.review.llm import LLMRequestError
from app.review.reviewers.contract import AdaptedOutput
from app.review.reviewers.executor import ReviewerExecutor
from app.review.reviewers.instructions import load_spec_instruction
from app.review.reviewers.schemas import ReviewCoverage

PRIVATE_OUTPUT = "private-review-raw-output-for-admin-only"
ERRORS = [{"path": "findings.1.segment_id", "code": "invalid_finding"}]


def write(store, **overrides):
    values = {
        "call_id": uuid4(),
        "project_id": uuid4(),
        "job_id": uuid4(),
        "reviewer": "logic",
        "instruction_version": "2026-11-12.3",
        "raw_output": PRIVATE_OUTPUT,
        "errors": ERRORS,
        **overrides,
    }
    store.write(**values)
    return values


def test_raw_and_per_item_errors_are_local_private_and_list_omits_contents(tmp_path):
    store = LocalReviewDebugStore(tmp_path / "debug")
    values = write(store)
    detail = store.get(values["call_id"])
    assert detail["raw_output"] == PRIVATE_OUTPUT and detail["errors"] == ERRORS
    assert detail["call_id"] == str(values["call_id"])
    assert detail["project_id"] == str(values["project_id"])
    assert detail["job_id"] == str(values["job_id"])
    assert datetime.fromisoformat(detail["expires_at"]) - datetime.fromisoformat(
        detail["created_at"]
    ) == timedelta(days=30)
    listed = store.list()
    assert len(listed) == 1 and "raw_output" not in listed[0] and "errors" not in listed[0]
    assert PRIVATE_OUTPUT not in json.dumps(listed)
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / f"{values['call_id']}.json").stat().st_mode) == 0o600


def test_expiry_is_exact_and_background_purge_does_not_require_page_visit(tmp_path):
    now = datetime(2026, 11, 12, tzinfo=UTC)
    store = LocalReviewDebugStore(tmp_path / "debug", clock=lambda: now)
    values = write(store)
    filename = store.root / f"{values['call_id']}.json"
    now += timedelta(days=30) - timedelta(microseconds=1)
    assert store.get(values["call_id"]) is not None and filename.exists()
    now += timedelta(microseconds=1)
    store.purge_expired()
    assert not filename.exists() and store.get(values["call_id"]) is None


def test_worker_cleans_expired_records_while_idle_and_logs_no_private_exception(
    tmp_path, monkeypatch, caplog
):
    from app import config, db

    removed, polls = [], []

    class Stop:
        def is_set(self):
            return len(polls) == 2

        def wait(self, seconds):
            pass

    class Store:
        def __init__(self, path):
            assert path == tmp_path / "review-debug"

        def purge_expired(self):
            removed.append(True)
            if len(removed) == 1:
                raise ReviewDebugError()

    monkeypatch.setattr(worker, "Event", Stop)
    monkeypatch.setattr(worker, "LocalReviewDebugStore", Store)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "run_once", lambda *args, **kwargs: polls.append(True) or False)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 3601.0 * len(polls))
    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(
            storage_dir=tmp_path,
            review_debug_path=tmp_path / "review-debug",
            review_job_timeout_seconds=1,
            review_job_lease_grace_seconds=1,
            review_job_max_attempts=1,
            review_job_retry_delay_seconds=1,
            worker_poll_seconds=1,
        ),
    )
    monkeypatch.setattr(db, "engine", SimpleNamespace(dispose=lambda: None))
    worker.main()
    assert len(removed) == len(polls) == 2
    assert "审核调试记录清理暂时失败" in caplog.text
    assert PRIVATE_OUTPUT not in caplog.text


def test_secrets_are_removed_from_raw_output_and_nested_error_values(tmp_path):
    store = LocalReviewDebugStore(tmp_path / "debug")
    values = write(
        store,
        raw_output=f'{{"leaked_key":"{PRIVATE_KEY}"}}',
        errors=[{"code": "invalid", "details": {PRIVATE_KEY: [PRIVATE_KEY]}}],
        secrets=[PRIVATE_KEY],
    )
    detail = store.get(values["call_id"])
    encoded = json.dumps(detail)
    assert PRIVATE_KEY not in encoded and "[REDACTED]" in encoded


def test_excessive_validation_errors_are_bounded_without_losing_original_output(tmp_path):
    errors = [
        {"path": f"findings.1.extra_{index}", "code": "extra_forbidden"} for index in range(1001)
    ]
    store = LocalReviewDebugStore(tmp_path)
    values = write(store, errors=errors)
    record = store.get(values["call_id"])
    assert record["raw_output"] == PRIVATE_OUTPUT
    assert record["errors"][:999] == errors[:999]
    assert record["errors"][999] == {"path": "$", "code": "errors_truncated", "total": 1001}
    assert len(record["errors"]) == 1000 and len(errors) == 1001


@pytest.mark.parametrize("kind", ["root_symlink", "record_symlink", "damaged_record"])
def test_store_never_follows_links_or_serves_damaged_records(tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalReviewDebugStore(tmp_path / "debug")
    if kind == "root_symlink":
        store.root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ReviewDebugError):
            write(store)
        assert list(outside.iterdir()) == []
        return
    values = write(store)
    filename = store.root / f"{values['call_id']}.json"
    if kind == "record_symlink":
        target = outside / "private.json"
        filename.rename(target)
        filename.symlink_to(target)
        assert store.get(values["call_id"]) is None
        assert PRIVATE_OUTPUT in target.read_text()
    else:
        filename.write_text('{"call_id":null}', encoding="utf-8")
        assert store.get(values["call_id"]) is None
    assert store.list() == []


@pytest.mark.parametrize("bad", ["../../private", "not-a-uuid", None, True])
def test_lookup_rejects_non_uuid_without_traversing_paths(tmp_path, bad):
    assert LocalReviewDebugStore(tmp_path).get(bad) is None


@pytest.mark.parametrize("failure", ["content_invalid", "output_truncated", "response_invalid"])
def test_model_structure_failures_capture_exact_raw_without_raw_in_ledger(
    tmp_path, ledger, failure
):
    raw = f"{PRIVATE_OUTPUT} {PRIVATE_KEY}"
    body = response_body(
        content=raw, finish_reason="length" if failure == "output_truncated" else "stop"
    )
    if failure == "response_invalid":
        body["choices"].append(body["choices"][0])
    store = LocalReviewDebugStore(tmp_path)
    instance = client(lambda request: httpx2.Response(200, json=body), debug_store=store)
    identity = uuid4()
    with pytest.raises(LLMRequestError) as rejected:
        complete(instance, call_id=identity)
    assert rejected.value.code == failure
    detail = store.get(identity)
    assert PRIVATE_OUTPUT in detail["raw_output"] and PRIVATE_KEY not in detail["raw_output"]
    assert detail["errors"] == [{"code": failure}]
    assert PRIVATE_OUTPUT not in json.dumps(ledger.settlements, default=str)
    assert instance._review_captures == {}


def test_semantic_failure_preserves_original_whitespace_and_individual_errors(tmp_path, ledger):
    raw = '{ "findings" : [ { "problem" : "保留现场" } ], "coverage_notes" : [] }'
    store = LocalReviewDebugStore(tmp_path)
    instance = client(
        lambda request: httpx2.Response(200, json=response_body(content=raw)), debug_store=store
    )
    identity, project, job = uuid4(), uuid4(), uuid4()
    assert complete(instance, call_id=identity, project_id=project, job_id=job) == json.loads(raw)
    instance.record_review_debug(
        call_id=identity,
        project_id=project,
        job_id=job,
        reviewer="spec_compliance",
        instruction_version="2026-11-12.3",
        errors=ERRORS,
    )
    assert store.get(identity)["raw_output"] == raw
    assert store.get(identity)["errors"] == ERRORS
    assert instance._review_captures == {}


def test_debug_write_failure_keeps_review_incomplete_after_valid_model_transport(ledger):
    class BrokenStore:
        def write(self, **values):
            raise OSError(PRIVATE_OUTPUT)

    instance = client(
        lambda request: httpx2.Response(
            200, json=response_body(content='{"findings":[],"coverage_notes":[]}')
        ),
        debug_store=BrokenStore(),
    )
    result = (
        ReviewerExecutor(instance)
        .run_compact(
            project_id=uuid4(),
            job_id=uuid4(),
            reviewer="spec_compliance",
            instruction=load_spec_instruction(),
            context={"plan": {}, "specification": {}, "delivery": {}},
            history=[],
            adapt_output=lambda output: AdaptedOutput(
                findings=(),
                coverage=ReviewCoverage(checked=("已读材料",), uncovered=()),
                errors=tuple(ERRORS),
            ),
        )
        .to_dict()
    )
    assert result["status"] == "incomplete" and result["error_code"] == "debug_record_failed"
    assert result["findings"] == [] and result["coverage"] is None
    assert len(result["attempts"]) == 1 and len(ledger.settlements) == 1
    assert ledger.settlements[0]["status"] == "succeeded"
    assert PRIVATE_OUTPUT not in json.dumps(result)
    assert instance._review_captures == {}


def test_single_json_repair_preserves_failed_original_and_releases_successful_raw(tmp_path, ledger):
    raw = PRIVATE_OUTPUT + " is not JSON"
    replies = iter((raw, '{"findings":[],"coverage_notes":[]}'))
    store = LocalReviewDebugStore(tmp_path)
    instance = client(
        lambda request: httpx2.Response(200, json=response_body(content=next(replies))),
        debug_store=store,
    )
    result = (
        ReviewerExecutor(instance)
        .run_compact(
            project_id=uuid4(),
            job_id=uuid4(),
            reviewer="spec_compliance",
            instruction=load_spec_instruction(),
            context={"plan": {}, "specification": {}, "delivery": {}},
            history=[],
            adapt_output=lambda output: AdaptedOutput(
                findings=(), coverage=ReviewCoverage(checked=("已读材料",), uncovered=())
            ),
        )
        .to_dict()
    )
    assert result["status"] == "completed" and len(result["attempts"]) == 2
    assert len(store.list()) == 1
    failed_call, successful_call = [entry["call_id"] for entry in result["attempts"]]
    assert store.get(failed_call)["raw_output"] == raw
    assert store.get(successful_call) is None
    assert PRIVATE_OUTPUT not in json.dumps(result)
    assert instance._review_captures == {}


def test_debug_pages_require_human_platform_admin_and_are_absent_from_project_export(
    auth_environment,
):
    team = ready_team(auth_environment)
    store = LocalReviewDebugStore(team.leader.app.state.settings.storage_dir / "review-debug")
    values = write(store, project_id=UUID(team.project["id"]))
    detail_path = f"/api/admin/review-debug/{values['call_id']}"
    for path in ("/api/admin/review-debug", detail_path):
        response = auth_environment.make_client().get(path)
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
        for person in (team.leader, team.member, team.other_member):
            response = person.get(path)
            assert response.status_code == 403 and PRIVATE_OUTPUT not in response.text
    outsider = auth_environment.make_client()
    admin_id = register(outsider, "debug-admin-outside-project").json()["id"]
    admin = auth_environment.make_client(platform_admin_user_ids=admin_id)
    assert login(admin, "debug-admin-outside-project").status_code == 200
    listing = admin.get("/api/admin/review-debug")
    assert listing.status_code == 200 and listing.json()["retention_days"] == 30
    assert listing.headers["cache-control"] == "no-store" and PRIVATE_OUTPUT not in listing.text
    detail = admin.get(detail_path)
    assert detail.status_code == 200 and detail.json()["raw_output"] == PRIVATE_OUTPUT
    assert detail.headers["cache-control"] == "no-store"
    assert admin.get(f"/api/projects/{team.project['id']}").status_code == 404
    agent = register_agent(team.leader, team.project["id"])
    for path in ("/api/admin/review-debug", detail_path):
        response = admin.get(path, headers=bearer(agent["token"]))
        assert response.status_code == 403 and PRIVATE_OUTPUT not in response.text
    for export_format in ("json", "csv"):
        response = team.leader.get(
            f"/api/projects/{team.project['id']}/events/export", params={"format": export_format}
        )
        assert response.status_code == 200 and PRIVATE_OUTPUT not in response.text
        assert str(values["call_id"]) not in response.text
    assert admin.get(f"/api/admin/review-debug/{uuid4()}").status_code == 404
    for invalid in ("0", "201", "bad"):
        response = admin.get("/api/admin/review-debug", params={"limit": invalid})
        assert response.status_code == 422 and PRIVATE_OUTPUT not in response.text


@pytest.mark.parametrize("override", [False, True])
def test_debug_default_and_override_use_same_directory_for_factory_and_admin_api(
    auth_environment, tmp_path, override
):
    root = tmp_path / ("persistent-debug" if override else "storage/review-debug")
    options = {"storage_dir": tmp_path / "storage"}
    if override:
        options["review_debug_dir"] = root
    settings = Settings(_env_file=None, **options)
    assert settings.review_debug_path == root
    model = build_llm_client(auth_environment.sessions, settings=settings)
    assert model._debug_store.root == root
    values = write(model._debug_store)
    person = auth_environment.make_client()
    identity = register(person, "override-debug-admin").json()["id"]
    admin = auth_environment.make_client(platform_admin_user_ids=identity, **options)
    assert login(admin, "override-debug-admin").status_code == 200
    response = admin.get(f"/api/admin/review-debug/{values['call_id']}")
    assert response.status_code == 200 and response.json()["raw_output"] == PRIVATE_OUTPUT
