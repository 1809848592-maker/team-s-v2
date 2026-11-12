"""正式交付文献核查：真实PG绑定和只读完整性，模型始终是内存替身。"""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select
from test_deliveries import ready_team, upload_delivery, upload_material
from test_draft_confirmation import confirm, draft_team
from test_llm_integration import FAKE_KEY, configuration, model_client
from test_llm_usage import KNOWN_USAGE, account_snapshot, assert_no_calls_or_admissions, budget
from test_modules import publish_modules
from test_specs import publish_spec, specification

from app.models import DeliveryVersion, DeliveryWorkflow, Event, ReviewJob
from app.models.llm_usage import ModelCall, ReviewAdmission
from app.review.citation_databases import CitationQuery, CitationVerification, ProviderEvidence
from app.review.extract_types import ExtractionError, ExtractionLimits
from app.review.llm import LLMClient
from app.review.model_config import ModelConfiguration
from app.review.reviewers.instructions import load_citation_instruction
from app.services import citation_review
from app.services.citation_review import CitationReviewError, review_delivery_citations
from app.services.llm_usage import QuotaExceededError, QuotaLimits
from app.storage import LocalStorage

PRIVATE = "private-citation-service-source-sentinel"
DATABASE_CLIENT = object()
HANDOFF = "private-citation-service-handoff-sentinel"


def storage_for(environment):
    return LocalStorage(environment.client.app.state.settings.storage_dir)


def upload(environment, team, content=PRIVATE.encode(), **changes):
    response = upload_delivery(team.member, team, content, handoff_note=HANDOFF, **changes)
    assert response.status_code == 201
    delivery = response.json()
    with environment.sessions() as db:
        job = db.scalar(select(ReviewJob).where(ReviewJob.delivery_id == UUID(delivery["id"])))
        assert job is not None
        delivery["test_job_id"] = str(job.id)
    return delivery


def review(
    environment,
    team,
    delivery,
    *,
    storage=None,
    client=None,
    database_client=DATABASE_CLIENT,
    history=(),
    limits=None,
):
    return review_delivery_citations(
        environment.sessions,
        project_id=UUID(team.project["id"]),
        delivery_id=UUID(delivery["id"]),
        job_id=UUID(delivery["test_job_id"]),
        storage=storage if storage is not None else storage_for(environment),
        client=client if client is not None else model_client(environment),
        database_client=database_client,
        history=history,
        limits=limits,
    )


def snapshot(environment):
    with environment.sessions() as db:
        return {
            model.__tablename__: [
                dict(row) for row in db.execute(select(model.__table__)).mappings()
            ]
            for model in (DeliveryVersion, DeliveryWorkflow, ReviewJob, Event)
        }


@pytest.fixture
def captured_core(monkeypatch):
    calls = []

    def core(client, **arguments):
        calls.append(arguments)
        return {"status": "completed", "findings": [], "coverage": {"uncovered": ["仅服务联测"]}}

    monkeypatch.setattr(citation_review, "review_citations", core)
    return calls


def test_bound_plan_spec_delivery_and_history_survive_newer_publications_without_writes(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    old = upload(auth_environment, team, b"old source\n\nold evidence")
    new_spec = specification("latest changes")
    new_spec["items"][0]["requirement"] = "LATEST_ONLY_SPEC_NOT_OLD"
    latest_spec = publish_spec(team.leader, team.project["id"], new_spec)
    latest_plan = publish_modules(team, team.modules, "latest plan note")
    new = upload(auth_environment, team, b"new source")
    before = snapshot(auth_environment)
    history = [{"not": "interpreted by service"}]
    result = review(auth_environment, team, old, history=history)
    old_arguments = captured_core[-1]
    assert result["status"] == "completed"
    assert result["delivery_id"] == old["id"] and result["delivery_version"] == 1
    assert result["job_id"] == old["test_job_id"]
    assert result["plan_version_id"] == team.plan["id"] and result["plan_version"] == 1
    assert result["spec_version_id"] == team.spec["id"] and result["spec_version"] == 1
    assert [s.text for s in old_arguments["document"].segments] == ["old source", "old evidence"]
    assert [s.location.line_start for s in old_arguments["document"].segments] == [1, 3]
    assert old_arguments["document"].sha256 == old["sha256"]
    assert old_arguments["specification"] == {"items": team.spec["items"]}
    assert old_arguments["database_client"] is DATABASE_CLIENT
    assert old_arguments["handoff_note"] == HANDOFF and old_arguments["history"] == history
    assert "LATEST_ONLY_SPEC_NOT_OLD" not in repr(old_arguments)
    fresh = review(auth_environment, team, new)
    assert fresh["spec_version_id"] == latest_spec["id"] and fresh["spec_version"] == 2
    assert fresh["plan_version_id"] == latest_plan["id"] and fresh["plan_version"] == 2
    assert fresh["delivery_version"] == 2
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_wrong_project_job_shared_material_and_unknown_never_open_storage_or_model(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    other = upload(auth_environment, team, b"other")
    shared = upload_material(team.member, team.project["id"]).json()

    class ForbiddenStorage:
        def path(self, digest):
            pytest.fail("Rejected formal scope must not reach storage")

    for project, delivery, job in [
        (uuid4(), UUID(formal["id"]), UUID(formal["test_job_id"])),
        (UUID(team.project["id"]), UUID(formal["id"]), UUID(other["test_job_id"])),
        (UUID(team.project["id"]), UUID(formal["id"]), uuid4()),
        (UUID(team.project["id"]), UUID(shared["id"]), UUID(formal["test_job_id"])),
        (UUID(team.project["id"]), uuid4(), UUID(formal["test_job_id"])),
    ]:
        with pytest.raises(CitationReviewError) as denied:
            review_delivery_citations(
                auth_environment.sessions,
                project_id=project,
                delivery_id=delivery,
                job_id=job,
                storage=ForbiddenStorage(),
                client=model_client(auth_environment),
                database_client=DATABASE_CLIENT,
                history=[],
            )
        assert denied.value.code == "source_unavailable"
    assert captured_core == []
    assert_no_calls_or_admissions(auth_environment)


def test_private_draft_is_not_read_but_exact_confirmed_formal_version_can_be_reviewed(
    auth_environment, captured_core
):
    team = draft_team(auth_environment)

    class ForbiddenStorage:
        def path(self, digest):
            pytest.fail("A private draft is not a formal review source")

    with pytest.raises(CitationReviewError) as denied:
        review_delivery_citations(
            auth_environment.sessions,
            project_id=UUID(team.project["id"]),
            delivery_id=UUID(team.draft["id"]),
            job_id=uuid4(),
            storage=ForbiddenStorage(),
            client=model_client(auth_environment),
            database_client=DATABASE_CLIENT,
            history=[],
        )
    assert denied.value.code == "source_unavailable" and captured_core == []
    formal = confirm(team).json()
    with auth_environment.sessions() as db:
        formal["test_job_id"] = str(
            db.scalar(select(ReviewJob.id).where(ReviewJob.delivery_id == UUID(formal["id"])))
        )
    result = review(auth_environment, team, formal)
    assert result["delivery_id"] == formal["id"]
    assert captured_core[-1]["document"].sha256 == team.draft["sha256"]


@pytest.mark.parametrize("change", ["missing", "same_length_corrupt", "truncated", "over_limit"])
def test_tampered_original_file_is_fixed_failure_before_model_without_state_writes(
    auth_environment, captured_core, change
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    storage = storage_for(auth_environment)
    path = storage.path(formal["sha256"])
    limits = None
    if change == "missing":
        path.unlink()
    elif change == "same_length_corrupt":
        path.write_bytes(b"x" * formal["size"])
    elif change == "truncated":
        path.write_bytes(b"short")
    else:
        path.write_bytes(b"x" * (formal["size"] + 10))
        limits = replace(ExtractionLimits(), max_file_bytes=formal["size"] + 1)
    before = snapshot(auth_environment)
    with pytest.raises(CitationReviewError) as error:
        review(auth_environment, team, formal, storage=storage, limits=limits)
    assert error.value.code == {
        "missing": "source_unavailable",
        "over_limit": "limit_exceeded",
    }.get(change, "source_mismatch")
    assert PRIVATE not in str(error.value) and str(path) not in str(error.value)
    assert captured_core == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_declared_size_limit_refuses_before_storage_and_db_connection_is_released(
    auth_environment, captured_core
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team)
    with auth_environment.sessions() as db:
        pool = db.get_bind().pool
    reads = []

    class InspectStorage:
        def path(self, digest) -> Path:
            assert pool.checkedout() == 0
            reads.append(digest)
            return storage_for(auth_environment).path(digest)

    with pytest.raises(CitationReviewError) as error:
        review(
            auth_environment,
            team,
            formal,
            storage=InspectStorage(),
            limits=replace(ExtractionLimits(), max_file_bytes=2),
        )
    assert error.value.code == "limit_exceeded" and not reads
    review(auth_environment, team, formal, storage=InspectStorage())
    assert reads == [formal["sha256"]] and len(captured_core) == 1


@pytest.mark.parametrize(
    "filename,data,code",
    [
        ("source.bin", b"unknown", "unsupported_format"),
        ("source.pdf", b"not PDF", "invalid_document"),
        ("source.md", b"\xff\xfe", "invalid_encoding"),
    ],
)
def test_unsupported_or_malformed_source_never_calls_model(
    auth_environment, captured_core, filename, data, code
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, data, filename=filename)
    before = snapshot(auth_environment)
    with pytest.raises(ExtractionError) as error:
        review(auth_environment, team, formal)
    assert error.value.code == code and captured_core == []
    assert snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


@pytest.mark.parametrize("limits", [{"max_text_chars": 2}, {"max_segments": 1}])
def test_extraction_output_limits_propagate_before_any_review_or_database_lookup(
    auth_environment, captured_core, limits
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, b"first\nsecond")
    before = snapshot(auth_environment)
    with pytest.raises(ExtractionError) as error:
        review(auth_environment, team, formal, limits=replace(ExtractionLimits(), **limits))
    assert error.value.code == "limit_exceeded"
    assert captured_core == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


BODY = PRIVATE + " manuscript claim [1]."
REFERENCE = "[1] Smith (2020). A Fixed Evidence Study. doi:10.1234/fixed-test"
SOURCE = (BODY + "\n" + REFERENCE).encode()


def location(line):
    return {
        "description": f"固定原文第{line}行",
        "part": "markdown",
        "line_start": line,
        "line_end": line,
    }


def extraction_output():
    return {
        "findings": [
            {
                "check_key": "reference",
                "severity": "suggestion",
                "segment_id": "S2",
                "problem": "识别到参考文献条目。",
                "excerpt": REFERENCE,
                "suggestion": "由平台提取并核验元数据。",
            }
        ],
        "coverage_notes": [],
    }


def alignment_output():
    return {
        "findings": [
            {
                "check_key": "support_insufficient",
                "severity": "important",
                "segment_id": "S1",
                "problem": "当前证据尚不能说明正文论断获得支撑。",
                "excerpt": BODY,
                "suggestion": "核对原文对该论断的支撑。",
                "context_key": "R1",
                "context_excerpt": REFERENCE,
            }
        ],
        "coverage_notes": ["论文全文支撑"],
    }


def model_response(output):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output, ensure_ascii=False)},
            }
        ],
        "usage": dict(KNOWN_USAGE),
    }


def citation_client(environment, *, transport, limits=None):
    profile = configuration().reviewers["spec_compliance"]
    return LLMClient(
        environment.sessions,
        configuration=ModelConfiguration(
            reviewers={"citation_extract": profile, "citation_alignment": profile}
        ),
        secrets={"TEST_ONLY_MODEL_KEY": FAKE_KEY},
        live_enabled=False,
        transport=transport,
        quota_limits=limits,
    )


class RecordingDatabase:
    def __init__(self, status="verified"):
        self.status = status
        self.queries = []

    def verify(self, query):
        assert isinstance(query, CitationQuery)
        self.queries.append(query)
        source_status = {"verified": "matched", "not_found": "no_match"}.get(
            self.status, "unavailable"
        )
        return CitationVerification(
            self.status,
            "固定数据库核验替身；不证明全文或论断真实性。",
            tuple(
                ProviderEvidence(provider, source_status, "固定测试结果。")
                for provider in ("crossref", "openalex")
            ),
        )


def assert_business_unchanged_and_private_accounting(environment, before):
    after = snapshot(environment)
    for table in ("delivery_versions", "delivery_workflows", "review_jobs"):
        assert after[table] == before[table]
    last_event_id = max(event["id"] for event in before["events"])
    encoded = json.dumps(account_snapshot(environment), ensure_ascii=False, default=str)
    encoded += json.dumps(
        [event for event in after["events"] if event["id"] > last_event_id],
        ensure_ascii=False,
        default=str,
    )
    for private in (PRIVATE, HANDOFF, FAKE_KEY):
        assert private not in encoded


@pytest.mark.parametrize("database_status", ["verified", "not_found"])
def test_real_two_stage_review_preserves_bound_versions_and_exact_accounting_sources(
    auth_environment, database_status
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, SOURCE)
    revised = specification("latest specification is not this delivery's bound version")
    revised["items"][0]["requirement"] = "LATEST_SPEC_MUST_NOT_REPLACE_OLD_BINDING"
    publish_spec(team.leader, team.project["id"], revised)
    publish_modules(team, team.modules, "LATEST_PLAN_MUST_NOT_REPLACE_OLD_BINDING")
    before = snapshot(auth_environment)
    database = RecordingDatabase(database_status)
    requests = {}
    outputs = {"citation_extract": extraction_output(), "citation_alignment": alignment_output()}
    unchanged = copy.deepcopy(outputs)

    def provider(request):
        payload = json.loads(request.content)
        with auth_environment.sessions() as db:
            pending = list(db.scalars(select(ModelCall).where(ModelCall.status == "reserved")))
            assert len(pending) == 1
            call = pending[0]
            assert db.get(ReviewAdmission, call.admission_id).call_count == len(requests) + 1
        assert request.headers["authorization"] == "Bearer " + FAKE_KEY
        encoded = json.dumps(payload["messages"], ensure_ascii=False)
        assert PRIVATE in encoded and HANDOFF in encoded
        assert team.spec["items"][0]["requirement"] in encoded
        assert "LATEST_SPEC_MUST_NOT_REPLACE_OLD_BINDING" not in encoded
        assert "LATEST_PLAN_MUST_NOT_REPLACE_OLD_BINDING" not in encoded
        requests[call.reviewer] = payload
        return httpx2.Response(200, json=model_response(outputs[call.reviewer]))

    result = review(
        auth_environment,
        team,
        formal,
        client=citation_client(auth_environment, transport=httpx2.MockTransport(provider)),
        database_client=database,
    )
    assert result["status"] == "completed" and result["error_code"] is None
    assert result["document_sha256"] == formal["sha256"]
    assert result["plan_version_id"] == team.plan["id"] and result["plan_version"] == 1
    assert result["spec_version_id"] == team.spec["id"] and result["spec_version"] == 1
    assert result["delivery_id"] == formal["id"] and result["job_id"] == formal["test_job_id"]
    assert set(requests) == {"citation_extract", "citation_alignment"}
    assert database.queries == [
        CitationQuery("A Fixed Evidence Study", ("Smith",), 2020, "10.1234/fixed-test", False)
    ]
    assert all(private not in repr(database.queries) for private in (PRIVATE, HANDOFF, FAKE_KEY))
    assert storage_for(auth_environment).path(formal["sha256"]).read_bytes() == SOURCE
    assert outputs == unchanged
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 2 and len({call.admission_id for call in calls}) == 1
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 2
        for call in calls:
            stage = result[
                {"citation_extract": "extraction", "citation_alignment": "alignment"}[call.reviewer]
            ]
            request = requests[call.reviewer]
            expected_hash = hashlib.sha256(
                json.dumps(request["messages"], ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
            assert stage["status"] == "completed" and len(stage["attempts"]) == 1
            assert stage["attempts"][0]["call_id"] == str(call.id)
            assert stage["attempts"][0]["input_hash"] == call.input_hash == expected_hash
            assert stage["instruction_version"] == call.instruction_version
            assert stage["instruction_version"] == load_citation_instruction(call.reviewer).version
            assert (
                stage["instruction_hash"]
                == hashlib.sha256(request["messages"][0]["content"].encode()).hexdigest()
            )
            assert stage["model"] == call.model == "fixed-test-model"
            assert call.status == "succeeded" and call.total_tokens == call.charged_tokens == 150
        bucket = budget(db, "platform", calls[0].day)
        assert (bucket.review_count, bucket.charged_tokens, bucket.reserved_tokens) == (1, 300, 0)
    extraction = result["extraction"]
    assert result["references"][0]["extraction_source"] == {
        key: extraction[key]
        for key in ("reviewer", "instruction_version", "model", "instruction_hash")
    } | {
        "call_id": extraction["attempts"][0]["call_id"],
        "input_hash": extraction["attempts"][0]["input_hash"],
    }
    finding = result["alignment"]["findings"][0]
    assert finding["severity"] == "important" and finding["reviewer"] == "citation_alignment"
    assert finding["call_id"] == result["alignment"]["attempts"][0]["call_id"]
    assert finding["input_hash"] == result["alignment"]["attempts"][0]["input_hash"]
    assert finding["instruction_hash"] == result["alignment"]["instruction_hash"]
    assert result["checks"][0]["citations"][0]["excerpt"] == BODY
    assert len(result["database_findings"]) == (database_status == "not_found")
    if database_status == "not_found":
        program = result["database_findings"][0]
        assert program["severity"] == "blocking" and program["source"] == "program"
        assert program["reviewer"] == "citation_database" and program["model"] is None
        assert program["call_id"] is None and program["document_sha256"] == formal["sha256"]
        assert program["basis"]["excerpt"] == REFERENCE
        assert program["verification"]["status"] == "not_found"
    assert "passed" not in result and "adopted" not in result
    assert_business_unchanged_and_private_accounting(auth_environment, before)


def test_initial_quota_propagates_before_model_or_database_without_business_writes(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, SOURCE)
    before = snapshot(auth_environment)
    database = RecordingDatabase()

    def forbidden(request):
        pytest.fail("Zero quota must deny before model transport")

    with pytest.raises(QuotaExceededError):
        review(
            auth_environment,
            team,
            formal,
            client=citation_client(
                auth_environment,
                transport=httpx2.MockTransport(forbidden),
                limits=QuotaLimits(project_daily_reviews=0),
            ),
            database_client=database,
        )
    assert database.queries == [] and snapshot(auth_environment) == before
    assert_no_calls_or_admissions(auth_environment)


def test_alignment_quota_propagates_after_settled_extraction_without_second_request(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, SOURCE)
    before = snapshot(auth_environment)
    database = RecordingDatabase()
    previews = {}

    class PreviewClient:
        configuration = ModelConfiguration(
            reviewers={
                reviewer: configuration().reviewers["spec_compliance"]
                for reviewer in ("citation_extract", "citation_alignment")
            }
        )

        def complete_json(self, **arguments):
            previews[arguments["reviewer"]] = arguments["messages"]
            return (
                extraction_output()
                if arguments["reviewer"] == "citation_extract"
                else alignment_output()
            )

    preview = review(
        auth_environment, team, formal, client=PreviewClient(), database_client=database
    )
    assert preview["status"] == "completed" and snapshot(auth_environment) == before
    reservations = {
        reviewer: len(json.dumps(messages, ensure_ascii=False, allow_nan=False).encode())
        + 1024
        + 128
        for reviewer, messages in previews.items()
    }
    first = reservations["citation_extract"]
    assert reservations["citation_alignment"] + 150 > first
    requests = []
    database.queries.clear()

    def provider(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=model_response(extraction_output()))

    with pytest.raises(QuotaExceededError) as denied:
        review(
            auth_environment,
            team,
            formal,
            client=citation_client(
                auth_environment,
                transport=httpx2.MockTransport(provider),
                limits=QuotaLimits(platform_daily_tokens=first),
            ),
            database_client=database,
        )
    assert denied.value.scope == "platform_tokens" and len(requests) == 1
    assert len(database.queries) == 1
    with auth_environment.sessions() as db:
        calls = list(db.scalars(select(ModelCall)))
        assert len(calls) == 1 and calls[0].reviewer == "citation_extract"
        assert calls[0].status == "succeeded" and calls[0].charged_tokens == 150
        assert db.get(ReviewAdmission, calls[0].admission_id).call_count == 1
        assert budget(db, "platform", calls[0].day).reserved_tokens == 0
    assert_business_unchanged_and_private_accounting(auth_environment, before)


def test_database_unavailable_keeps_model_sources_and_returns_incomplete_without_business_writes(
    auth_environment,
):
    team = ready_team(auth_environment)
    formal = upload(auth_environment, team, SOURCE)
    before = snapshot(auth_environment)
    database = RecordingDatabase("unverified")
    outputs = [extraction_output(), alignment_output()]

    def provider(request):
        return httpx2.Response(200, json=model_response(outputs.pop(0)))

    result = review(
        auth_environment,
        team,
        formal,
        client=citation_client(auth_environment, transport=httpx2.MockTransport(provider)),
        database_client=database,
    )
    assert result["status"] == "incomplete" and result["error_code"] == "database_unavailable"
    assert result["extraction"]["status"] == result["alignment"]["status"] == "completed"
    assert result["references"][0]["verification"]["status"] == "unverified"
    assert result["database_findings"] == [] and result["checks"] == []
    assert any("未能核验" in text for text in result["coverage"]["uncovered"])
    assert not outputs
    assert_business_unchanged_and_private_accounting(auth_environment, before)
