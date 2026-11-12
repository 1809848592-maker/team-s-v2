"""M4-02: use the actual HTTP, PostgreSQL and model adapter with fixed model output."""

import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import func, select
from test_auth import CSRF_HEADERS
from test_llm_transport import response_body
from test_llm_usage import KNOWN_USAGE
from test_mcp import mcp_network as mcp_network
from test_modules import create_team

from app.config import Settings
from app.models import ModelCall, Module, PlanVersion, ReviewAdmission
from app.review.factory import build_llm_client
from app.services import model_disclosure


def body(team, **changes):
    a, b = str(uuid4()), str(uuid4())
    content = {
        "research_question": "政策能否改善就业？",
        "final_deliverable": "研究报告",
        "argument": "识别政策对就业的因果影响，再提出建议。",
        "modules": [
            {
                "id": a,
                "name": "数据整理",
                "assignee_ids": [team.member_id],
                "goal": "整理就业数据",
                "scope_in": "收集和清理数据",
                "scope_out": "不做因果识别",
                "downstream_handoff": "清洗后的数据表",
                "delivery_format": "md",
            },
            {
                "id": b,
                "name": "政策建议",
                "assignee_ids": [team.leader_id],
                "goal": "提出政策建议",
                "scope_in": "整理数据并给出建议",
                "scope_out": "",
                "upstream": [{"module_id": a, "needs": "因果效应估计值"}],
                "delivery_format": "md",
            },
        ],
    }
    return {
        "request_id": str(uuid4()),
        "disclosure_version": model_disclosure.DISCLOSURE_VERSION,
        "content": content,
        **changes,
    }


def finding(key="dependency", **changes):
    return {
        "check_key": key,
        "severity": "important",
        "segment_id": "S3",
        "problem": "上游只承诺清洗数据，无法提供下游所需的因果估计。",
        "excerpt": "因果效应估计值",
        "suggestion": "补充分工并明确估计结果的交接。",
        **changes,
    }


def model(environment, monkeypatch, outputs=None, *, effect=None, **limits):
    from app.api import plan_self_check

    attempts = []
    responses = (
        outputs if outputs is not None else [{"findings": [finding()], "coverage_notes": []}]
    )

    def transport(request):
        attempts.append(json.loads(request.content))
        if effect:
            effect(request)
        value = responses[min(len(attempts) - 1, len(responses) - 1)]
        return httpx2.Response(
            200,
            json=response_body(
                content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                usage=KNOWN_USAGE,
            ),
        )

    settings = Settings(
        _env_file=None,
        deepseek_api_key="fixed-self-check-key",
        storage_dir=environment.client.app.state.settings.storage_dir,
        **limits,
    )
    client = build_llm_client(
        environment.sessions, settings, transport=httpx2.MockTransport(transport)
    )
    monkeypatch.setattr(plan_self_check, "build_llm_client", lambda *_: client)
    return attempts, client


def submit(team, value=None, *, client=None, headers=None):
    return (client or team.leader).post(
        f"/api/projects/{team.project['id']}/plan/self-check",
        json=body(team) if value is None else value,
        headers=CSRF_HEADERS if headers is None else headers,
    )


def count(environment, entity):
    with environment.sessions() as db:
        return db.scalar(select(func.count()).select_from(entity))


def test_self_check_current_unpublished_draft_returns_located_advice_without_publishing(
    auth_environment,
    monkeypatch,
):
    team = create_team(auth_environment)
    # First RED exercises the missing public behavior, before installing its adapter.
    from app.api import plan_self_check  # noqa: F401

    attempts, _ = model(auth_environment, monkeypatch)
    value = body(team)
    response = submit(team, value)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["status"] == "completed"
    assert result["findings"][0]["module_id"] == value["content"]["modules"][1]["id"]
    assert result["findings"][0]["location"] == "模块：政策建议"
    assert result["findings"][0]["excerpt_verified"] is True
    assert {c["key"]: c["status"] for c in result["checks"]} == {
        "dependency": "issues",
        "omission": "checked",
        "overlap": "checked",
        "alignment": "checked",
    }
    assert (
        len(attempts)
        == count(auth_environment, ModelCall)
        == count(auth_environment, ReviewAdmission)
        == 1
    )
    assert count(auth_environment, PlanVersion) == count(auth_environment, Module) == 0
    with auth_environment.sessions() as db:
        call = db.scalar(select(ModelCall))
        assert call.reviewer == result["reviewer"] == "plan_self_check"
        assert (
            call.input_hash
            == result["input_hash"]
            == hashlib.sha256(
                json.dumps(attempts[0]["messages"], ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest()
        )
        assert call.total_tokens == 150 and call.estimated_cost_cny is not None
    # Publication is a separate human action, regardless of self-check opinions.
    published = team.leader.post(
        f"/api/projects/{team.project['id']}/plan/versions",
        json={"change_note": "负责人确认后发布", "content": value["content"]},
        headers=CSRF_HEADERS,
    )
    assert published.status_code == 201, published.text
    assert count(auth_environment, PlanVersion) == 1 and len(attempts) == 1


def test_four_review_dimensions_keep_valid_items_and_mark_unverified_excerpts(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    outputs = [
        {
            "findings": [
                finding(),
                finding("omission", segment_id="S1", excerpt="研究报告"),
                finding("overlap", excerpt="整理数据并给出建议"),
                finding("alignment", excerpt="模型编造的摘录"),
                finding(segment_id="S99"),
                {"nonsense": True},
            ],
            "coverage_notes": [],
        }
    ]
    attempts, client = model(auth_environment, monkeypatch, outputs)
    response = submit(team)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(attempts) == 1 and len(result["findings"]) == 4
    assert result["discarded_findings"] == 2
    assert all(c["status"] == "issues" for c in result["checks"])
    assert result["findings"][3]["excerpt_verified"] is False
    assert any("2 条意见因格式无效" in n for n in result["coverage_notes"])
    records = client._debug_store.list()
    assert len(records) == 1
    record = client._debug_store.get(records[0]["call_id"])
    assert any(e["code"] == "unknown_segment" for e in record["errors"])
    assert "模型编造的摘录" in record["raw_output"]


@pytest.mark.parametrize(
    "first", ["invalid-json", {"findings": [finding(segment_id="S90")], "coverage_notes": []}]
)
def test_one_structure_repair_shares_one_allowance_and_records_actual_final_input(
    auth_environment, monkeypatch, first
):
    team = create_team(auth_environment)
    attempts, _ = model(
        auth_environment, monkeypatch, [first, {"findings": [finding()], "coverage_notes": []}]
    )
    response = submit(team)
    assert response.status_code == 200, response.text
    assert len(attempts) == count(auth_environment, ModelCall) == 2
    with auth_environment.sessions() as db:
        from app.models import DailyBudget

        admission = db.scalar(select(ReviewAdmission))
        assert admission.call_count == 2 and admission.job_id is None
        assert all(
            b.review_count == 1 and b.charged_tokens == 300 for b in db.scalars(select(DailyBudget))
        )
    assert (
        response.json()["input_hash"]
        == hashlib.sha256(
            json.dumps(attempts[-1]["messages"], ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    )


def test_twice_invalid_output_is_unfinished_and_publishing_still_works_without_check(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    attempts, client = model(
        auth_environment, monkeypatch, [{"findings": [{}], "coverage_notes": []}]
    )
    value = body(team)
    response = submit(team, value)
    assert response.status_code == 502 and "未完成" in response.text
    assert len(attempts) == len(client._debug_store.list()) == 2
    assert count(auth_environment, PlanVersion) == 0
    assert (
        team.leader.post(
            f"/api/projects/{team.project['id']}/plan/versions",
            json={
                "change_note": "不以自检结果阻止发布",
                "content": value["content"],
            },
            headers=CSRF_HEADERS,
        ).status_code
        == 201
    )


def test_no_modules_skips_model_and_missing_materials_are_uncovered(auth_environment, monkeypatch):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch, [{"findings": [], "coverage_notes": []}])
    value = body(team)
    value["content"]["modules"] = []
    response = submit(team, value)
    assert response.status_code == 200 and response.json()["status"] == "incomplete"
    assert all(c["status"] == "uncovered" for c in response.json()["checks"])
    assert attempts == [] and count(auth_environment, ModelCall) == 0
    value = body(team)
    value["content"]["argument"] = ""
    value["content"]["modules"][0]["scope_in"] = ""
    response = submit(team, value)
    assert response.status_code == 200 and response.json()["status"] == "incomplete"
    assert {c["key"] for c in response.json()["checks"] if c["status"] == "uncovered"} == {
        "omission",
        "overlap",
        "alignment",
    }


@pytest.mark.parametrize("issue", ["dangling", "cycle"])
def test_dependencies_can_be_self_checked_before_publication_validation(
    auth_environment, monkeypatch, issue
):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    value = body(team)
    modules = value["content"]["modules"]
    modules[0]["upstream"] = [
        {"module_id": str(uuid4()) if issue == "dangling" else modules[1]["id"], "needs": "结果"}
    ]
    assert submit(team, value).status_code == 200
    assert len(attempts) == 1
    published = team.leader.post(
        f"/api/projects/{team.project['id']}/plan/versions",
        json={
            "change_note": "发布仍执行原有校验",
            "content": value["content"],
        },
        headers=CSRF_HEADERS,
    )
    assert published.status_code == 422
    assert count(auth_environment, Module) == count(auth_environment, PlanVersion) == 0


def test_duplicate_request_and_changed_draft_never_repeat_external_calls(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    value = body(team)
    assert submit(team, value).status_code == 200
    assert submit(team, value).status_code == 409
    value["content"]["argument"] = "changed"
    assert submit(team, value).status_code == 409
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "limits",
    [
        {"llm_project_daily_reviews": 0},
        {"llm_platform_daily_reviews": 0},
        {"llm_platform_daily_tokens": 1},
    ],
)
def test_existing_quota_stops_self_check_before_transport(auth_environment, monkeypatch, limits):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch, **limits)
    response = submit(team)
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    assert "未完成" in response.text
    assert attempts == [] and count(auth_environment, ModelCall) == 0


def test_http_releases_db_connection_and_project_lock_during_call(auth_environment, monkeypatch):
    from app.models import Project

    team = create_team(auth_environment)
    engine = auth_environment.sessions.kw["bind"]
    observed = []

    def check(_):
        observed.append(engine.pool.checkedout())
        assert observed[-1] == 0
        with auth_environment.sessions() as db:
            assert db.scalar(
                select(Project.id)
                .where(Project.id == UUID(team.project["id"]))
                .with_for_update(nowait=True)
            )

    model(auth_environment, monkeypatch, effect=check)
    assert submit(team).status_code == 200
    assert observed == [0]


def test_leader_revocation_during_call_keeps_usage_but_hides_result(auth_environment, monkeypatch):
    from app.models import Event, ProjectMember

    team = create_team(auth_environment)

    def revoke(_):
        with auth_environment.sessions() as db:
            db.get(ProjectMember, (UUID(team.project["id"]), UUID(team.leader_id))).role = "member"
            db.commit()

    model(auth_environment, monkeypatch, effect=revoke)
    assert submit(team).status_code == 403
    with auth_environment.sessions() as db:
        assert db.scalar(select(ModelCall)).total_tokens == 150
        assert not list(
            db.scalars(select(Event).where(Event.event_type == "plan_self_check_finished"))
        )


def test_only_current_human_leader_can_check_not_admin_or_members(auth_environment, monkeypatch):
    from test_auth import login, register

    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    admin = auth_environment.make_client(platform_admin_user_ids=team.member_id)
    assert login(admin, "member").status_code == 200
    outsider = auth_environment.make_client()
    register(outsider, "outside")
    for person, status in (
        (team.member, 403),
        (team.other_member, 403),
        (admin, 403),
        (outsider, 404),
        (auth_environment.make_client(), 401),
    ):
        assert submit(team, client=person).status_code == status
    assert attempts == []


@pytest.mark.parametrize("cookie", [False, True])
def test_agent_cannot_check_even_with_leader_cookie_and_denial_is_audited(
    auth_environment, monkeypatch, cookie
):
    from test_agents import bearer, register_agent

    from app.models import Event

    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    agent = register_agent(team.leader, team.project["id"])
    response = submit(
        team,
        client=team.leader if cookie else auth_environment.make_client(),
        headers={**CSRF_HEADERS, **bearer(agent["token"])},
    )
    assert response.status_code == 403 and attempts == []
    with auth_environment.sessions() as db:
        denied = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert denied.details["action"] == "plan_self_check"


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-CSRF-Protection": "0"}, {**CSRF_HEADERS, "Origin": "https://attacker.invalid"}],
)
def test_csrf_is_required(auth_environment, monkeypatch, headers):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    assert submit(team, headers=headers).status_code == 403 and attempts == []


def test_consent_and_current_notice_required_before_any_model_budget(auth_environment, monkeypatch):
    from test_model_disclosure import unconsented_team

    team = unconsented_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    assert submit(team).status_code == 409
    team.leader.post(
        f"/api/projects/{team.project['id']}/model-consent",
        json={
            "disclosure_version": model_disclosure.DISCLOSURE_VERSION,
        },
        headers=CSRF_HEADERS,
    )
    assert submit(team, body(team, disclosure_version="old-version")).status_code == 409
    assert attempts == [] and count(auth_environment, ModelCall) == 0
    notice = team.leader.get(f"/api/projects/{team.project['id']}/model-disclosure").json()
    assert "未发布方案" in notice["notice"]["summary"]


@pytest.mark.parametrize(
    "issue",
    ["duplicate_module", "foreign_assignee", "extra_source", "control_character", "oversize"],
)
def test_invalid_or_foreign_drafts_rejected_before_transport(auth_environment, monkeypatch, issue):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch)
    value = body(team)
    if issue == "duplicate_module":
        value["content"]["modules"][1]["id"] = value["content"]["modules"][0]["id"]
    elif issue == "foreign_assignee":
        value["content"]["modules"][0]["assignee_ids"] = [str(uuid4())]
    elif issue == "extra_source":
        value["model"] = "untrusted-model"
    elif issue == "control_character":
        value["content"]["argument"] = "bad\x01input"
    else:
        value["content"]["modules"] = [
            {
                **deepcopy(value["content"]["modules"][0]),
                "id": str(uuid4()),
                "scope_in": "内容" * 5000,
            }
            for _ in range(80)
        ]
    assert submit(team, value).status_code == 422
    assert attempts == [] and count(auth_environment, ModelCall) == 0


def test_network_failure_never_retries_or_changes_plan_and_keeps_unknown_usage(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)

    def timeout(request):
        raise httpx2.ReadTimeout("fixed timeout", request=request)

    attempts, _ = model(auth_environment, monkeypatch, effect=timeout)
    response = submit(team)
    assert response.status_code == 502 and "未完成" in response.text
    assert len(attempts) == 1 and count(auth_environment, PlanVersion) == 0
    with auth_environment.sessions() as db:
        assert db.scalar(select(ModelCall)).status == "unknown"


def test_request_provenance_is_immutable_and_usage_rejects_other_sources(
    auth_environment, monkeypatch
):
    from sqlalchemy import update

    from app.models import PlanSelfCheckRequest
    from app.models.immutable import SnapshotImmutableError
    from app.services.llm_usage import LLMUsageError

    team = create_team(auth_environment)
    value = body(team)
    attempts, client = model(auth_environment, monkeypatch)
    assert submit(team, value).status_code == 200
    with auth_environment.sessions() as db:
        source = db.get(PlanSelfCheckRequest, UUID(value["request_id"]))
        assert source.project_id == UUID(team.project["id"])
        with pytest.raises(SnapshotImmutableError):
            db.execute(update(PlanSelfCheckRequest).values(content_hash="f" * 64))
        db.rollback()
    for kwargs in (
        {"plan_self_check_request_id": uuid4(), "reviewer": "plan_self_check"},
        {"plan_self_check_request_id": UUID(value["request_id"]), "reviewer": "logic"},
        {"plan_self_check_request_id": UUID(value["request_id"]), "reviewer": "plan_self_check"},
    ):
        with pytest.raises(LLMUsageError):
            client.complete_json(
                project_id=UUID(team.project["id"]),
                job_id=None,
                instruction_version="v1",
                messages=[{"role": "user", "content": "Return JSON for wrong source"}],
                call_id=uuid4(),
                **kwargs,
            )
    assert len(attempts) == 1


def test_self_check_admission_cannot_be_attached_to_delivery_job(auth_environment, monkeypatch):
    from test_deliveries import ready_team
    from test_llm_usage import formal_job

    from app.services.llm_usage import LLMUsageError

    team = ready_team(auth_environment)
    job = formal_job(auth_environment, team)
    attempts, client = model(auth_environment, monkeypatch)
    with pytest.raises(LLMUsageError):
        client.complete_json(
            project_id=job.project_id,
            job_id=job.job_id,
            reviewer="plan_self_check",
            instruction_version="v1",
            messages=[{"role": "user", "content": "Return JSON: a delivery is not a plan draft"}],
        )
    assert attempts == []


def test_calls_are_in_admin_csv_and_debug_is_admin_only(auth_environment, monkeypatch):
    import csv
    import io
    from datetime import UTC, datetime

    from test_auth import login

    team = create_team(auth_environment)
    attempts, client = model(
        auth_environment, monkeypatch, [{"findings": [finding(), {}], "coverage_notes": []}]
    )
    assert submit(team).status_code == 200
    call_id = client._debug_store.list()[0]["call_id"]
    path = f"/api/admin/review-debug/{call_id}"
    assert team.leader.get(path).status_code == team.member.get(path).status_code == 403
    admin = auth_environment.make_client(platform_admin_user_ids=team.leader_id)
    assert login(admin, "leader").status_code == 200
    debug = admin.get(path)
    assert debug.status_code == 200 and "raw_output" in debug.json()
    assert debug.headers["cache-control"] == "no-store"
    day = datetime.now(UTC).date().isoformat()
    report = admin.get(f"/api/admin/usage?start={day}&end={day}")
    exported = admin.get(f"/api/admin/usage.csv?start={day}&end={day}")
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))
    assert len(attempts) == len(rows) == 1
    assert rows[0]["reviewer"] == "plan_self_check"
    assert int(rows[0]["total_tokens"]) == 150 and rows[0]["estimated_cost_cny"]
    assert all("raw_output" not in row for row in rows)
    assert report.status_code == 200


def test_concurrent_duplicate_self_check_has_one_outbound_call(auth_environment, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.schemas.plan_self_check import PlanSelfCheckGenerate
    from app.services import plan_self_check

    team = create_team(auth_environment)
    value = PlanSelfCheckGenerate.model_validate(body(team))
    attempts, client = model(auth_environment, monkeypatch)
    barrier = Barrier(2)

    def invoke():
        barrier.wait(timeout=5)
        try:
            return plan_self_check.generate(
                auth_environment.sessions,
                UUID(team.project["id"]),
                UUID(team.leader_id),
                value,
                client=client,
            )
        except plan_self_check.SelfCheckDuplicateError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=15) for f in [pool.submit(invoke), pool.submit(invoke)]]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert len(attempts) == count(auth_environment, ModelCall) == 1


def test_real_calls_stay_disabled_even_with_a_key(auth_environment):
    from test_auth import login

    team = create_team(auth_environment)
    client = auth_environment.make_client(
        deepseek_api_key="fixed-not-real-key", llm_enable_live_calls=False
    )
    assert login(client, "leader").status_code == 200
    response = submit(team, client=client)
    assert response.status_code == 503 and "fixed-not-real-key" not in response.text
    assert count(auth_environment, ModelCall) == 0


def test_missing_reviewer_configuration_is_503_and_keeps_published_plan(
    auth_environment, monkeypatch
):
    team = create_team(auth_environment)
    _, client = model(auth_environment, monkeypatch)
    configuration = client.configuration.model_dump(mode="json")
    del configuration["reviewers"]["plan_self_check"]
    from app.review.model_config import ModelConfiguration

    client.configuration = ModelConfiguration.model_validate(configuration)
    response = submit(team)
    assert response.status_code == 503
    assert count(auth_environment, ModelCall) == 0


def test_configured_call_limit_does_not_become_server_error(auth_environment, monkeypatch):
    team = create_team(auth_environment)
    attempts, _ = model(auth_environment, monkeypatch, ["invalid-json"], llm_max_calls_per_review=1)
    response = submit(team)
    assert response.status_code == 502 and "未完成" in response.text
    assert len(attempts) == 1


@pytest.mark.parametrize("cookie", [False, True])
def test_real_mcp_does_not_expose_self_check_to_leader_owned_agent(
    auth_environment, mcp_network, cookie
):
    import asyncio

    from test_agents import register_agent
    from test_mcp import official_client, wait_for_records

    from app.models import Event, PlanSelfCheckRequest
    from app.services.auth import COOKIE_NAME

    team = create_team(auth_environment)
    agent = register_agent(team.leader, team.project["id"])
    headers = {"Cookie": f"{COOKIE_NAME}={team.leader.cookies.get(COOKIE_NAME)}"} if cookie else {}

    async def scenario():
        async with official_client(mcp_network, agent["token"], headers=headers) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "plan_self_check" not in names
            result = await client.call_tool(
                "plan_self_check", {"project_id": team.project["id"], **body(team)}
            )
            assert result.is_error

    asyncio.run(scenario())
    records = wait_for_records(auth_environment, team, "agent_tool_call", 1)
    assert records[0].details == {"tool": "unknown", "outcome": "error"}
    with auth_environment.sessions() as db:
        denied = db.scalar(select(Event).where(Event.event_type == "agent_permission_denied"))
        assert denied.details["action"] == "mcp_unknown_tool"
    assert count(auth_environment, PlanSelfCheckRequest) == count(auth_environment, ModelCall) == 0
