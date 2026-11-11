"""固定流水线的调度、事务和领取隔离；无真实模型或外部数据库API。"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from test_review_jobs import claim, database_time, queued_delivery
from test_reviewer_executor import historical

from app.models import DeliveryWorkflow, ReviewJob
from app.review.reviewers.schemas import HistoricalOpinion
from app.review.runner import DeliveryReviewInput
from app.services import review_pipeline
from app.services.llm_usage import QuotaExceededError

STAGES = ("format", "specification", "citations", "reasoning")
OPERATIONS = (
    "inspect_delivery_format",
    "review_delivery_spec",
    "review_delivery_citations",
    "review_delivery_reasoning",
)
PRIVATE = "PRIVATE_EXCEPTION_CONTENT_OR_API_KEY_NEVER_IN_REPORT"


def review_input():
    return DeliveryReviewInput(uuid4(), uuid4(), uuid4(), 1, 1)


def history():
    return [HistoricalOpinion.model_validate_json(json.dumps(historical()))]


def install_stages(monkeypatch, *, failed=None, failure=None, malformed=None):
    calls = []
    outputs = {}
    for name, operation in zip(STAGES, OPERATIONS, strict=True):
        outputs[name] = {"status": "completed", "private_raw_component": name, "findings": []}

        def execute(sessions, *, _name=name, **arguments):
            calls.append((_name, sessions, arguments))
            if _name == failed:
                raise failure
            return malformed if _name == "format" and malformed is not None else outputs[_name]

        monkeypatch.setattr(review_pipeline, operation, execute)
    return calls, outputs


def install_report_capture(monkeypatch):
    from app.services import review_reports

    captures = []
    pointer = {"report_id": str(uuid4())}

    def stage(sessions, review, components):
        captures.append((sessions, review, components))
        return pointer

    monkeypatch.setattr(review_reports, "stage_report", stage)
    return captures, pointer


def invoke(sessions, review, *, storage=None, client=None, database_client=None):
    return review_pipeline.execute_pipeline(
        sessions,
        review,
        storage=storage,
        client=client,
        database_client=database_client,
    )


def test_four_stages_share_exact_context_history_and_preserve_complete_large_raw_reports(
    monkeypatch,
):
    old, review, sessions = history(), review_input(), object()
    monkeypatch.setattr(review_pipeline, "_load_history", lambda supplied, task: old)
    calls, outputs = install_stages(monkeypatch)
    outputs["reasoning"]["large_diagnostic"] = "片段" * 100_000
    captures, pointer = install_report_capture(monkeypatch)
    storage, client, database_client = object(), object(), object()
    result = invoke(
        sessions, review, storage=storage, client=client, database_client=database_client
    )
    assert result is pointer and len(json.dumps(result).encode()) < 100
    assert [call[0] for call in calls] == list(STAGES)
    assert len(captures) == 1 and captures[0][:2] == (sessions, review)
    assert captures[0][2] == outputs
    assert all(captures[0][2][name] is outputs[name] for name in STAGES)
    for name, supplied, arguments in calls:
        assert supplied is sessions
        assert arguments["project_id"] == review.project_id
        assert arguments["delivery_id"] == review.delivery_id
        assert arguments["storage"] is storage
        if name == "format":
            assert {"job_id", "client", "history", "database_client"}.isdisjoint(arguments)
        else:
            assert arguments["job_id"] == review.job_id
            assert arguments["client"] is client and arguments["history"] is old
            assert ("database_client" in arguments) == (name == "citations")
            if name == "citations":
                assert arguments["database_client"] is database_client


@pytest.mark.parametrize("failed", STAGES)
def test_each_stage_exception_is_safe_partial_and_does_not_cancel_independent_stages(
    monkeypatch, failed
):
    monkeypatch.setattr(review_pipeline, "_load_history", lambda supplied, task: [])
    calls, outputs = install_stages(monkeypatch, failed=failed, failure=RuntimeError(PRIVATE))
    captures, pointer = install_report_capture(monkeypatch)
    assert invoke(object(), review_input()) is pointer
    assert [call[0] for call in calls] == list(STAGES)
    report = captures[0][2]
    assert report[failed] == {
        "status": "incomplete",
        "error_code": "execution_error",
        "summary": "此审核步骤未完成，请稍后重试。",
    }
    assert PRIVATE not in json.dumps(report)
    assert all(report[name] is outputs[name] for name in STAGES if name != failed)


@pytest.mark.parametrize("failed", STAGES)
def test_quota_is_unchanged_and_stops_further_calls_without_staging(monkeypatch, failed):
    monkeypatch.setattr(review_pipeline, "_load_history", lambda supplied, task: [])
    reset = datetime.now(UTC) + timedelta(days=1)
    failure = QuotaExceededError("platform_tokens", reset)
    calls, _ = install_stages(monkeypatch, failed=failed, failure=failure)
    captures, _ = install_report_capture(monkeypatch)
    with pytest.raises(QuotaExceededError) as error:
        invoke(object(), review_input())
    assert error.value is failure and error.value.retry_at == reset
    assert [call[0] for call in calls] == list(STAGES[: STAGES.index(failed) + 1])
    assert captures == []


@pytest.mark.parametrize("malformed", [[], "not a report", 42])
def test_non_object_stage_output_is_a_partial_failure(monkeypatch, malformed):
    monkeypatch.setattr(review_pipeline, "_load_history", lambda supplied, task: [])
    calls, _ = install_stages(monkeypatch, malformed=malformed)
    captures, _ = install_report_capture(monkeypatch)
    invoke(object(), review_input())
    assert len(calls) == 4
    assert captures[0][2]["format"]["status"] == "incomplete"


def claimed_review(environment):
    team, delivery = queued_delivery(environment)
    claimed = claim(environment, database_time(environment), timeout_seconds=120)
    assert claimed is not None
    return (
        team,
        delivery,
        DeliveryReviewInput(
            claimed.job_id,
            claimed.delivery_id,
            claimed.project_id,
            claimed.attempt,
            claimed.workflow_revision,
        ),
    )


def test_real_claim_is_checked_and_history_connection_closes_before_all_stages(
    auth_environment, monkeypatch
):
    from app.services import review_reports

    _, delivery, review = claimed_review(auth_environment)
    old, loaded = history(), []
    with auth_environment.sessions() as db:
        pool = db.get_bind().pool

    def load(db, item):
        assert str(item.id) == delivery["id"] and item.project_id == review.project_id
        assert pool.checkedout() == 1
        loaded.append(item.id)
        return old

    monkeypatch.setattr(review_reports, "load_review_history", load)
    calls, _ = install_stages(monkeypatch)
    for operation in OPERATIONS:
        original = getattr(review_pipeline, operation)

        def outside(*args, _original=original, **kwargs):
            assert pool.checkedout() == 0
            return _original(*args, **kwargs)

        monkeypatch.setattr(review_pipeline, operation, outside)
    captures, pointer = install_report_capture(monkeypatch)
    assert invoke(auth_environment.sessions, review) is pointer
    assert loaded == [UUID(delivery["id"])] and len(captures) == 1
    assert all(call[2]["history"] is old for call in calls[1:])


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": uuid4()},
        {"delivery_id": uuid4()},
        {"job_id": uuid4()},
        {"attempt": 2},
        {"workflow_revision": 2},
        {"attempt": True},
        {"attempt": 0},
        {"workflow_revision": True},
        {"workflow_revision": 0},
        {"project_id": "not a UUID"},
    ],
)
def test_wrong_or_malformed_claim_never_loads_history_opens_storage_calls_models_or_stages(
    auth_environment, monkeypatch, changes
):
    from app.services import review_reports

    _, _, review = claimed_review(auth_environment)
    monkeypatch.setattr(
        review_reports, "load_review_history", lambda *args: pytest.fail("invalid history access")
    )
    calls, _ = install_stages(monkeypatch)
    captures, _ = install_report_capture(monkeypatch)
    with pytest.raises(review_pipeline.PipelineSourceError):
        invoke(auth_environment.sessions, replace(review, **changes))
    assert calls == captures == []


@pytest.mark.parametrize("mutation", ["queued", "expired", "state", "job_revision"])
def test_inactive_or_stale_database_claim_stops_before_history_and_checks(
    auth_environment, monkeypatch, mutation
):
    from app.services import review_reports

    _, _, review = claimed_review(auth_environment)
    with auth_environment.sessions() as db:
        if mutation == "queued":
            db.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review.job_id)
                .values(status="queued", claim_token=None, lease_until=None)
            )
        elif mutation == "expired":
            db.execute(
                update(ReviewJob)
                .where(ReviewJob.id == review.job_id)
                .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif mutation == "state":
            db.execute(
                update(DeliveryWorkflow)
                .where(DeliveryWorkflow.delivery_id == review.delivery_id)
                .values(state="returned")
            )
        else:
            db.execute(
                update(ReviewJob).where(ReviewJob.id == review.job_id).values(workflow_revision=2)
            )
        db.commit()
    monkeypatch.setattr(
        review_reports, "load_review_history", lambda *args: pytest.fail("invalid history access")
    )
    calls, _ = install_stages(monkeypatch)
    captures, _ = install_report_capture(monkeypatch)
    with pytest.raises(review_pipeline.PipelineSourceError):
        invoke(auth_environment.sessions, review)
    assert calls == captures == []


def test_history_load_failure_does_not_run_or_stage_any_component(monkeypatch):
    def fail(*args):
        raise review_pipeline.PipelineSourceError

    monkeypatch.setattr(review_pipeline, "_load_history", fail)
    calls, _ = install_stages(monkeypatch)
    captures, _ = install_report_capture(monkeypatch)
    with pytest.raises(review_pipeline.PipelineSourceError):
        invoke(object(), review_input())
    assert calls == captures == []


def test_actual_four_services_disabled_models_persist_partial_report_without_false_approval(
    auth_environment,
):
    from sqlalchemy import func, select

    from app.config import Settings
    from app.models import ModelCall, ReviewAdmission
    from app.models.review_report import ReviewReport, ReviewSummary
    from app.review.factory import build_llm_client
    from app.services.review_jobs import JobClaim, complete_job
    from app.storage import LocalStorage

    _, delivery, review = claimed_review(auth_environment)
    with auth_environment.sessions() as db:
        job = db.get(ReviewJob, review.job_id)
        claimed = JobClaim(
            job.id,
            review.delivery_id,
            review.project_id,
            job.claim_token,
            job.attempt,
            job.workflow_revision,
        )

    class NoDatabaseRequests:
        def verify(self, query):
            pytest.fail("Disabled model cannot cause external reference database requests")

    pointer = invoke(
        auth_environment.sessions,
        review,
        storage=LocalStorage(auth_environment.client.app.state.settings.storage_dir),
        client=build_llm_client(
            auth_environment.sessions,
            Settings(_env_file=None, llm_enable_live_calls=False, deepseek_api_key=""),
        ),
        database_client=NoDatabaseRequests(),
    )
    assert set(pointer) == {"report_id"}
    with auth_environment.sessions() as db:
        report = db.get(ReviewReport, UUID(pointer["report_id"]))
        assert report.delivery_id == UUID(delivery["id"])
        assert set(report.components) == set(STAGES)
        assert report.components["format"]["status"] == "unsupported"
        for name in STAGES[1:]:
            assert report.components[name]["status"] == "incomplete"
            assert report.components[name]["document_sha256"] == delivery["sha256"]
        assert db.get(DeliveryWorkflow, review.delivery_id).state == "ai_reviewing"
        assert db.scalar(select(func.count()).select_from(ReviewSummary)) == 0
        assert complete_job(db, claimed, pointer)
        db.commit()
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, review.delivery_id).state == "ai_review_partial_failure"
        assert db.get(ReviewJob, review.job_id).status == "retry_wait"
        assert db.scalar(select(ReviewSummary.status)) == "incomplete"
        assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
        assert db.scalar(select(func.count()).select_from(ReviewAdmission)) == 0


def test_actual_four_services_and_five_mock_model_calls_reach_final_review_with_exact_sources(
    auth_environment, monkeypatch
):
    import hashlib

    import httpx2
    from sqlalchemy import func, select
    from test_llm_integration import FAKE_KEY, configuration
    from test_llm_usage import KNOWN_USAGE, account_snapshot
    from test_reasoning_review_service import model_response, output_for, request_data

    from app.models import ModelCall, ReviewAdmission
    from app.models.review_report import ReviewFinding, ReviewReport, ReviewSummary
    from app.review.llm import LLMClient
    from app.review.model_config import ModelConfiguration
    from app.services.review_jobs import JobClaim, complete_job
    from app.storage import LocalStorage

    _, delivery, review = claimed_review(auth_environment)
    storage = LocalStorage(auth_environment.client.app.state.settings.storage_dir)
    original = storage.path(delivery["sha256"]).read_bytes()
    with auth_environment.sessions() as db:
        pool = db.get_bind().pool
        job = db.get(ReviewJob, review.job_id)
        claimed = JobClaim(
            job.id,
            review.delivery_id,
            review.project_id,
            job.claim_token,
            job.attempt,
            job.workflow_revision,
        )
    reviewers = ("spec_compliance", "citation_extract", "citation_alignment", "logic", "linkage")
    requests = {}
    database_queries = []

    class NoDatabaseRequests:
        def verify(self, query):
            database_queries.append(query)
            pytest.fail("An absent bibliography cannot cause any external database request")

    def no_real_http(*args, **kwargs):
        pytest.fail("All model requests must use the in-memory MockTransport")

    monkeypatch.setattr(httpx2, "HTTPTransport", no_real_http)

    def provider(request):
        # 五个真实审核员都经同一个正式job记账，外发期间没有持有业务事务。
        assert pool.checkedout() == 0
        with auth_environment.sessions() as db:
            reserved = list(db.scalars(select(ModelCall).where(ModelCall.status == "reserved")))
            assert len(reserved) == 1
            call = reserved[0]
            admission = db.get(ReviewAdmission, call.admission_id)
            assert admission.job_id == review.job_id
            assert admission.call_count == len(requests) + 1
            reviewer, call_id = call.reviewer, str(call.id)
        assert reviewer == reviewers[len(requests)] and reviewer not in requests
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        payload = json.loads(request.content)
        assert request_data(payload, "historical_handling") == []
        assert request_data(payload, "delivery")["sha256"] == delivery["sha256"]
        requests[reviewer] = (call_id, payload)
        coverage = {"checked": ["固定替身只核对所给可读文字"], "uncovered": ["原始数据与外部全文"]}
        if reviewer == "spec_compliance":
            output = {
                "findings": [],
                "coverage": coverage,
                "checks": [
                    {
                        "spec_number": item["number"],
                        "status": "checked",
                        "reason": "固定替身已核对本条与可读正文。",
                    }
                    for item in request_data(payload, "bound_specification")["items"]
                ],
            }
        elif reviewer == "citation_extract":
            output = {
                "findings": [],
                "coverage": coverage,
                "references": [],
                "bibliography_status": "absent",
            }
        elif reviewer == "citation_alignment":
            output = {"findings": [], "coverage": coverage, "checks": [], "issue_links": []}
        else:
            output = output_for(payload, reviewer, finding=False)
        return httpx2.Response(200, json=model_response(output))

    profile = configuration().reviewers["spec_compliance"]
    client = LLMClient(
        auth_environment.sessions,
        configuration=ModelConfiguration(reviewers={name: profile for name in reviewers}),
        secrets={"TEST_ONLY_MODEL_KEY": FAKE_KEY},
        live_enabled=False,
        transport=httpx2.MockTransport(provider),
    )
    pointer = invoke(
        auth_environment.sessions,
        review,
        storage=storage,
        client=client,
        database_client=NoDatabaseRequests(),
    )
    assert set(pointer) == {"report_id"} and tuple(requests) == reviewers
    assert database_queries == []
    assert storage.path(delivery["sha256"]).read_bytes() == original
    with auth_environment.sessions() as db:
        report = db.get(ReviewReport, UUID(pointer["report_id"]))
        assert report.components["format"]["status"] == "unsupported"
        assert report.components["format"]["sha256"] == delivery["sha256"]
        for name in STAGES[1:]:
            component = report.components[name]
            assert component["status"] == "completed"
            assert component["document_sha256"] == delivery["sha256"]
            assert component["job_id"] == str(review.job_id)
            assert component["delivery_id"] == delivery["id"]
            assert component["plan_version_id"] == delivery["plan_version_id"]
            assert component["spec_version_id"] == delivery["spec_version_id"]
        components = report.components
        results = {
            "spec_compliance": components["specification"]["review"],
            "citation_extract": components["citations"]["extraction"],
            "citation_alignment": components["citations"]["alignment"],
            "logic": components["reasoning"]["logic"],
            "linkage": components["reasoning"]["linkage"],
        }
        for reviewer, result in results.items():
            call_id, payload = requests[reviewer]
            call = db.get(ModelCall, UUID(call_id))
            assert call.status == "succeeded"
            assert result["status"] == "completed" and result["findings"] == []
            assert len(result["attempts"]) == 1
            attempt = result["attempts"][0]
            assert attempt["call_id"] == call_id
            expected_input_hash = hashlib.sha256(
                json.dumps(payload["messages"], ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
            assert attempt["input_hash"] == call.input_hash == expected_input_hash
            assert (
                result["instruction_hash"]
                == hashlib.sha256(payload["messages"][0]["content"].encode()).hexdigest()
            )
            assert result["reviewer"] == call.reviewer == reviewer
            assert result["model"] == call.model == profile.model
            assert result["instruction_version"] == call.instruction_version
            assert call.total_tokens == KNOWN_USAGE["total_tokens"]
        admissions = list(db.scalars(select(ReviewAdmission)))
        assert len(admissions) == 1 and admissions[0].call_count == len(reviewers)
        assert db.scalar(select(func.sum(ModelCall.total_tokens))) == 750
        assert db.get(DeliveryWorkflow, review.delivery_id).state == "ai_reviewing"
        assert complete_job(db, claimed, pointer)
        db.commit()
    with auth_environment.sessions() as db:
        assert db.get(DeliveryWorkflow, review.delivery_id).state == "pending_final_review"
        assert db.get(ReviewJob, review.job_id).status == "succeeded"
        summary = db.scalar(select(ReviewSummary))
        assert summary.status == "completed" and not summary.nonconverged
        assert db.scalar(select(func.count()).select_from(ReviewFinding)) == 0
    assert FAKE_KEY not in json.dumps(account_snapshot(auth_environment), default=str)


def test_production_entry_lazily_builds_explicit_clients_and_storage(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app import config, db
    from app.review import citation_databases, factory, runner
    from app.storage import LocalStorage

    settings = SimpleNamespace(storage_dir=tmp_path)
    sessions, model, database = object(), object(), object()
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "SessionLocal", sessions)
    builds = []

    def build(supplied, configuration):
        builds.append((supplied, configuration))
        return model

    monkeypatch.setattr(factory, "build_llm_client", build)
    monkeypatch.setattr(citation_databases, "CitationDatabaseClient", lambda: database)
    received = []

    def execute(supplied, review, **arguments):
        received.append((supplied, review, arguments))
        return {"report_id": "fixed-reference"}

    monkeypatch.setattr(review_pipeline, "execute_pipeline", execute)
    review = review_input()
    assert runner.run_delivery_review(review) == {"report_id": "fixed-reference"}
    assert builds == [(sessions, settings)] and received[0][:2] == (sessions, review)
    assert received[0][2]["client"] is model
    assert received[0][2]["database_client"] is database
    storage = received[0][2]["storage"]
    assert isinstance(storage, LocalStorage) and storage.root == tmp_path
