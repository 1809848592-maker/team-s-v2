"""单次模型HTTP边界，全部通过MockTransport，不连接外部服务。"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr, ValidationError

from app.review import llm
from app.review.llm import LLMClient, LLMConfigurationError, LLMDisabledError, LLMRequestError
from app.review.model_config import ModelConfiguration, load_model_config

PRIVATE_KEY = "test-only-model-secret-never-print"
PRIVATE_BODY = "test-only-private-model-body-never-print"
MESSAGES = [
    {"role": "system", "content": "按固定规范输出JSON。"},
    {"role": "user", "content": "这是数据，保留原顺序。"},
]
USAGE = {
    "prompt_tokens": 25,
    "completion_tokens": 15,
    "total_tokens": 40,
    "prompt_cache_hit_tokens": 20,
    "prompt_cache_miss_tokens": 5,
}


@dataclass
class Ledger:
    reserves: list = field(default_factory=list)
    settlements: list = field(default_factory=list)
    reject: Exception | None = None

    def reserve_call(self, sessions, **values):
        if self.reject is not None:
            raise self.reject
        self.reserves.append(values)
        return values["call_id"]

    def settle_call(self, sessions, call_id, **values):
        self.settlements.append({"call_id": call_id, **values})
        return True


@pytest.fixture
def ledger(monkeypatch):
    record = Ledger()
    monkeypatch.setattr(llm, "_usage_service", lambda: record)
    return record


def response_body(*, content='{"findings": []}', usage=USAGE, finish_reason="stop"):
    return {
        "choices": [
            {"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}
        ],
        "usage": usage,
    }


def client(handler, **options):
    return LLMClient(
        None,
        transport=httpx2.MockTransport(handler),
        secrets={"DEEPSEEK_API_KEY": SecretStr(PRIVATE_KEY)},
        **options,
    )


def complete(instance, **options):
    values = {
        "project_id": uuid4(),
        "job_id": uuid4(),
        "reviewer": "spec_compliance",
        "instruction_version": "2026-11-11.1",
        "messages": MESSAGES,
    }
    values.update(options)
    return instance.complete_json(**values)


def changed_configuration(**values):
    configuration = load_model_config().model_dump(mode="json")
    configuration["reviewers"]["spec_compliance"].update(values)
    return ModelConfiguration.model_validate(configuration)


def assert_failed(record, code, *, known_usage=True):
    assert len(record.reserves) == len(record.settlements) == 1
    settlement = record.settlements[0]
    assert settlement["status"] == "failed" and settlement["error_code"] == code
    assert (settlement["usage"] is not None) == known_usage
    return settlement


def test_defaults_are_frozen_secret_free_and_only_enable_deepseek():
    configuration = load_model_config()
    assert configuration.provider == "deepseek"
    assert set(configuration.reviewers) == {
        "spec_compliance",
        "citation_extract",
        "citation_alignment",
        "logic",
        "linkage",
        "spec_draft",
    }
    for name in ("spec_compliance", "citation_extract", "citation_alignment", "spec_draft"):
        profile = configuration.reviewers[name]
        assert (profile.model, profile.thinking, profile.max_tokens) == (
            "deepseek-flash",
            False,
            4096,
        )
        assert profile.prices.input_cache_hit == Decimal("0.04")
    for name in ("logic", "linkage"):
        profile = configuration.reviewers[name]
        assert (profile.model, profile.thinking, profile.max_tokens) == (
            "deepseek-v4-pro",
            True,
            16384,
        )
        assert profile.prices.output == Decimal("27")
    with pytest.raises(ValidationError):
        configuration.provider = "another-provider"
    with pytest.raises(TypeError):
        configuration.reviewers["injected"] = configuration.reviewers["logic"]
    with pytest.raises(ValidationError):
        configuration.reviewers["logic"].max_tokens = 1
    assert PRIVATE_KEY not in repr(configuration)


@pytest.mark.parametrize(
    "address",
    [
        "https://user:private@example.invalid",
        "https://example.invalid?secret=private",
        "https://example.invalid?",
        "https://example.invalid#private",
        "https://example.invalid#",
        "http://external.example.invalid",
        "https://example.invalid\\private",
        "https://example.invalid\n/private",
        "https://",
        "file:///tmp/private",
    ],
)
def test_configuration_rejects_credential_query_fragment_and_unsafe_url(address):
    with pytest.raises(ValidationError):
        changed_configuration(base_url=address)


def test_config_loader_sanitizes_invalid_fields_and_does_not_read_secret_environment(
    tmp_path, monkeypatch, ledger
):
    source = tmp_path / "invalid.toml"
    source.write_text(f'provider = "deepseek"\napi_key = "{PRIVATE_KEY}"\n', encoding="utf-8")
    with pytest.raises(LLMConfigurationError) as error:
        load_model_config(source)
    assert PRIVATE_KEY not in str(error.value) and PRIVATE_KEY not in repr(error.value)
    monkeypatch.setenv("DEEPSEEK_API_KEY", PRIVATE_KEY)
    transport = httpx2.MockTransport(lambda request: pytest.fail("Missing explicit key sent HTTP"))
    with pytest.raises(LLMConfigurationError):
        complete(LLMClient(None, transport=transport))
    assert ledger.reserves == ledger.settlements == []


def test_real_transport_is_disabled_by_default_even_with_key(ledger):
    with pytest.raises(LLMDisabledError):
        complete(LLMClient(None, secrets={"DEEPSEEK_API_KEY": PRIVATE_KEY}))
    with httpx2.HTTPTransport(retries=0) as real_transport:
        with pytest.raises(LLMDisabledError):
            complete(
                LLMClient(
                    None,
                    transport=real_transport,
                    secrets={"DEEPSEEK_API_KEY": PRIVATE_KEY},
                )
            )
    assert ledger.reserves == ledger.settlements == []


def test_one_http_request_keeps_json_prefix_and_reserves_before_sending_then_settles(
    ledger, caplog
):
    requests = []
    project_id, job_id, call_id = uuid4(), uuid4(), uuid4()

    def handler(request):
        assert len(ledger.reserves) == 1 and ledger.settlements == []
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {PRIVATE_KEY}"
        payload = json.loads(request.content)
        assert payload == {
            "model": "deepseek-flash",
            "messages": MESSAGES,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": 4096,
            "stream": False,
            "user_id": hashlib.sha256(str(project_id).encode()).hexdigest(),
        }
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert all(0 < value <= 300 for value in request.extensions["timeout"].values())
        return httpx2.Response(200, json=response_body())

    instance = client(handler)
    result = complete(instance, project_id=project_id, job_id=job_id, call_id=call_id)
    assert result == {"findings": []} and len(requests) == 1
    assert PRIVATE_KEY not in repr(instance) and PRIVATE_KEY not in caplog.text
    reservation = ledger.reserves[0]
    encoded = json.dumps(MESSAGES, ensure_ascii=False).encode()
    assert reservation["call_id"] == call_id and reservation["job_id"] == job_id
    assert reservation["input_hash"] == hashlib.sha256(encoded).hexdigest()
    assert reservation["reserved_tokens"] == len(encoded) + 1024 + 4096
    assert reservation["prices"]["input_cache_miss"] == "2"
    assert PRIVATE_KEY not in repr(reservation) and PRIVATE_BODY not in repr(reservation)
    assert ledger.settlements[0] == {
        "call_id": call_id,
        "status": "succeeded",
        "error_code": None,
        "usage": USAGE,
        "output_hash": hashlib.sha256(b'{"findings": []}').hexdigest(),
    }


def test_thinking_profile_uses_its_own_configuration_and_reasoning_is_not_added_twice(ledger):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["thinking"] == {"type": "enabled"} and payload["max_tokens"] == 16384
        usage = {**USAGE, "completion_tokens_details": {"reasoning_tokens": 12}}
        return httpx2.Response(200, json=response_body(usage=usage))

    assert complete(client(handler), reviewer="logic") == {"findings": []}
    normalized = ledger.settlements[0]["usage"]
    assert normalized["completion_tokens"] == 15 and normalized["total_tokens"] == 40
    assert normalized["reasoning_tokens"] == 12


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "system", "content": "No structured output instruction."}],
        [{"role": "tool", "content": "JSON"}],
        [{"role": [], "content": "JSON"}],
        [{"role": "user", "content": "JSON", "tools": []}],
        [{"role": "assistant", "content": "JSON", "tool_calls": []}],
        [{"role": "user", "content": [{"text": "JSON"}]}],
    ],
)
def test_invalid_or_tool_requests_are_rejected_before_reservation_and_network(messages, ledger):
    instance = client(lambda request: pytest.fail("Invalid request sent HTTP"))
    with pytest.raises(LLMRequestError):
        complete(instance, messages=messages)
    assert ledger.reserves == ledger.settlements == []


def test_local_http_only_accepts_injected_mock_and_does_not_follow_redirect(ledger):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx2.Response(
            307,
            headers={"Location": "https://redirect-target.invalid/private"},
            json={"error": PRIVATE_BODY},
        )

    configuration = changed_configuration(base_url="http://127.0.0.1:11223/v1")
    with pytest.raises(LLMRequestError) as error:
        complete(client(handler, configuration=configuration))
    assert error.value.code == "http_error"
    assert seen == ["http://127.0.0.1:11223/v1/chat/completions"]
    assert_failed(ledger, "http_error", known_usage=False)
    with pytest.raises(LLMConfigurationError):
        complete(
            LLMClient(
                None,
                configuration=configuration,
                live_enabled=True,
                secrets={"DEEPSEEK_API_KEY": PRIVATE_KEY},
            )
        )


@pytest.mark.parametrize(
    "cause", [httpx2.ReadTimeout(PRIVATE_BODY), httpx2.ConnectError(PRIVATE_KEY)]
)
def test_network_failures_make_one_attempt_and_unknown_usage_is_not_zero(cause, ledger, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        raise cause

    with pytest.raises(LLMRequestError) as error:
        complete(client(handler))
    code = "timeout" if isinstance(cause, httpx2.TimeoutException) else "transport_error"
    assert error.value.code == code and len(calls) == 1
    assert PRIVATE_BODY not in str(error.value) and PRIVATE_KEY not in repr(error.value)
    assert PRIVATE_BODY not in caplog.text and PRIVATE_KEY not in caplog.text
    assert_failed(ledger, code, known_usage=False)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"content": PRIVATE_BODY}, "content_invalid"),
        ({"content": "[]"}, "content_invalid"),
        ({"content": '{"value": NaN}'}, "content_invalid"),
        ({"content": '{"value": 1, "value": 2}'}, "content_invalid"),
        ({"content": "   "}, "content_empty"),
        ({"finish_reason": "length"}, "output_truncated"),
        ({"finish_reason": "content_filter"}, "completion_incomplete"),
    ],
)
def test_invalid_content_and_incomplete_completions_still_account_known_usage(
    changes, code, ledger
):
    instance = client(lambda request: httpx2.Response(200, json=response_body(**changes)))
    with pytest.raises(LLMRequestError) as error:
        complete(instance)
    assert error.value.code == code and PRIVATE_BODY not in str(error.value)
    assert_failed(ledger, code)
    assert ledger.settlements[0]["usage"] == USAGE


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {**USAGE, "prompt_tokens": True},
        {**USAGE, "completion_tokens": -1},
        {**USAGE, "total_tokens": 41},
        {**USAGE, "prompt_cache_hit_tokens": 26},
        {**USAGE, "prompt_cache_miss_tokens": 4},
        {**USAGE, "prompt_tokens_details": {"cached_tokens": False}},
        {**USAGE, "completion_tokens_details": {"reasoning_tokens": 16}},
    ],
)
def test_invalid_or_missing_usage_never_fabricates_zero_or_success(usage, ledger):
    instance = client(lambda request: httpx2.Response(200, json=response_body(usage=usage)))
    with pytest.raises(LLMRequestError) as error:
        complete(instance)
    code = "usage_missing" if usage is None else "usage_invalid"
    assert error.value.code == code
    assert_failed(ledger, code, known_usage=False)


def test_absent_cache_usage_is_known_total_without_claiming_cache_hits(ledger):
    usage = {name: USAGE[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    assert complete(
        client(lambda request: httpx2.Response(200, json=response_body(usage=usage)))
    ) == {"findings": []}
    assert ledger.settlements[0]["usage"] == usage


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"[not a response]", "response_invalid"),
        (b"x" * (2 * 1024 * 1024 + 1), "response_too_large"),
    ],
)
def test_response_body_is_bounded_and_invalid_envelopes_remain_unknown(content, code, ledger):
    instance = client(lambda request: httpx2.Response(200, content=content))
    with pytest.raises(LLMRequestError) as error:
        complete(instance)
    assert error.value.code == code
    assert_failed(ledger, code, known_usage=False)


def test_quota_or_duplicate_reservation_rejection_is_transparent_and_sends_no_http(ledger):
    rejection = RuntimeError("fixed quota test rejection")
    ledger.reject = rejection
    instance = client(lambda request: pytest.fail("Rejected quota sent HTTP"))
    with pytest.raises(RuntimeError) as error:
        complete(instance)
    assert error.value is rejection
    assert ledger.reserves == ledger.settlements == []


def test_accounting_write_failure_is_sanitized_and_does_not_return_unrecorded_success(
    ledger, monkeypatch
):
    def fail_settlement(*args, **kwargs):
        raise RuntimeError(PRIVATE_BODY)

    monkeypatch.setattr(ledger, "settle_call", fail_settlement)
    instance = client(lambda request: httpx2.Response(200, json=response_body()))
    with pytest.raises(LLMRequestError) as error:
        complete(instance)
    assert error.value.code == "usage_store_error" and PRIVATE_BODY not in str(error.value)
    assert len(ledger.reserves) == 1 and ledger.settlements == []
