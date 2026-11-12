"""有界的单次OpenAI格式调用；先持久预留，再外发，最后独立结算。"""

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx2
from pydantic import SecretStr

from app.review.model_config import (
    LLMConfigurationError,
    ModelConfiguration,
    load_model_config,
)

if TYPE_CHECKING:
    from app.services.llm_usage import QuotaLimits

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class LLMDisabledError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("平台尚未启用真实模型调用。")


class LLMRequestError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("模型处理未完成，请稍后重试。")


def _usage_service():
    from app.services import llm_usage

    return llm_usage


def _json_object(raw: str | bytes) -> dict:
    def reject_constant(value):
        raise ValueError

    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError
            result[name] = value
        return result

    value = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError
    return value


def _token_count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _validated_usage(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError
    usage = {
        name: _token_count(value[name])
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
    if usage["total_tokens"] != prompt + completion:
        raise ValueError
    hit, miss = None, None
    if "prompt_cache_hit_tokens" in value:
        hit = _token_count(value["prompt_cache_hit_tokens"])
    if "prompt_cache_miss_tokens" in value:
        miss = _token_count(value["prompt_cache_miss_tokens"])
    if "prompt_tokens_details" in value:
        details = value["prompt_tokens_details"]
        if not isinstance(details, dict):
            raise ValueError
        if "cached_tokens" in details:
            cached = _token_count(details["cached_tokens"])
            if hit is not None and hit != cached:
                raise ValueError
            hit = cached
    if hit is not None or miss is not None:
        hit = prompt - miss if hit is None else hit
        miss = prompt - hit if miss is None else miss
        if hit < 0 or miss < 0 or hit + miss != prompt:
            raise ValueError
        usage.update(prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=miss)
    if "completion_tokens_details" in value:
        details = value["completion_tokens_details"]
        if not isinstance(details, dict):
            raise ValueError
        if "reasoning_tokens" in details:
            reasoning = _token_count(details["reasoning_tokens"])
            if reasoning > completion:
                raise ValueError
            # 推理token已在completion_tokens中，只保留分项，不能再相加计费。
            usage["reasoning_tokens"] = reasoning
    return usage


class LLMClient:
    def __init__(
        self,
        sessions,
        *,
        configuration: ModelConfiguration | None = None,
        secrets: Mapping[str, str | SecretStr] | None = None,
        live_enabled: bool = False,
        transport: httpx2.BaseTransport | None = None,
        quota_limits: "QuotaLimits | None" = None,
    ) -> None:
        self._sessions = sessions
        self.configuration = configuration if configuration is not None else load_model_config()
        self._secrets = dict(secrets) if secrets is not None else {}
        self._live_enabled = live_enabled
        self._transport = transport
        self._quota_limits = quota_limits

    def complete_json(
        self,
        *,
        project_id: UUID,
        job_id: UUID | None,
        reviewer: str,
        instruction_version: str,
        messages: list[dict],
        call_id: UUID | None = None,
        spec_draft_request_id: UUID | None = None,
    ) -> dict:
        mock = isinstance(self._transport, httpx2.MockTransport)
        if not self._live_enabled and not mock:
            raise LLMDisabledError
        if not isinstance(reviewer, str):
            raise LLMConfigurationError
        profile = self.configuration.reviewers.get(reviewer)
        if profile is None or (urlsplit(profile.base_url).scheme != "https" and not mock):
            raise LLMConfigurationError
        secret = self._secrets.get(profile.key_env)
        key = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if not isinstance(key, str) or not key or any(ord(char) <= 32 for char in key):
            raise LLMConfigurationError
        if (
            not isinstance(project_id, UUID)
            or (job_id is None) == (spec_draft_request_id is None)
            or (job_id is not None and not isinstance(job_id, UUID))
            or (spec_draft_request_id is not None and not isinstance(spec_draft_request_id, UUID))
            or (call_id is not None and not isinstance(call_id, UUID))
            or not isinstance(instruction_version, str)
            or not instruction_version
            or len(instruction_version) > 80
            or not isinstance(messages, list)
            or not messages
            or len(messages) > 128
        ):
            raise LLMRequestError("request_invalid")
        for message in messages:
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or not isinstance(message["role"], str)
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str)
            ):
                raise LLMRequestError("request_invalid")
        if not any("json" in message["content"].casefold() for message in messages):
            raise LLMRequestError("json_instruction_required")
        try:
            encoded = json.dumps(messages, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise LLMRequestError("request_invalid") from None
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise LLMRequestError("request_too_large")
        # 保留固定system/规范/方案前缀及其顺序，使厂商可以复用上下文缓存。
        payload = {
            "model": profile.model,
            "messages": json.loads(encoded),
            "response_format": {"type": "json_object"},
            "thinking": {"type": "enabled" if profile.thinking else "disabled"},
            "max_tokens": profile.max_tokens,
            "stream": False,
            "user_id": hashlib.sha256(str(project_id).encode()).hexdigest(),
        }
        identity = call_id if call_id is not None else uuid4()
        usage_service = _usage_service()
        # 配额/重复call异常直接透传；这里尚未打开HTTP连接。
        source = {"spec_draft_request_id": spec_draft_request_id} if spec_draft_request_id else {}
        usage_service.reserve_call(
            self._sessions,
            project_id=project_id,
            job_id=job_id,
            call_id=identity,
            reviewer=reviewer,
            instruction_version=instruction_version,
            model=profile.model,
            endpoint=profile.endpoint,
            input_hash=hashlib.sha256(encoded).hexdigest(),
            reserved_tokens=len(encoded) + 1024 + profile.max_tokens,
            prices=profile.prices.model_dump(mode="json"),
            limits=self._quota_limits,
            **source,
        )
        usage, result, output_hash, code = None, None, None, None
        try:
            with httpx2.Client(
                transport=self._transport,
                timeout=profile.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    "POST",
                    profile.endpoint,
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    json=payload,
                ) as response:
                    content = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise LLMRequestError("response_too_large")
                        content.extend(chunk)
                    try:
                        body = _json_object(bytes(content))
                    except (ValueError, UnicodeError, RecursionError):
                        raise LLMRequestError("response_invalid") from None
                    try:
                        usage = _validated_usage(body.get("usage"))
                    except (KeyError, ValueError, TypeError):
                        usage = None
                    if not 200 <= response.status_code < 300:
                        raise LLMRequestError("http_error")
                    if usage is None:
                        raise LLMRequestError(
                            "usage_missing" if body.get("usage") is None else "usage_invalid"
                        )
                    choices = body.get("choices")
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise LLMRequestError("response_invalid")
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        raise LLMRequestError("response_invalid")
                    if choice.get("finish_reason") == "length":
                        raise LLMRequestError("output_truncated")
                    if choice.get("finish_reason") != "stop":
                        raise LLMRequestError("completion_incomplete")
                    message = choice.get("message")
                    if (
                        not isinstance(message, dict)
                        or message.get("role") != "assistant"
                        or message.get("tool_calls")
                        or message.get("function_call")
                    ):
                        raise LLMRequestError("response_invalid")
                    text = message.get("content")
                    if not isinstance(text, str) or not text.strip():
                        raise LLMRequestError("content_empty")
                    output_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    try:
                        result = _json_object(text)
                    except (ValueError, UnicodeError, RecursionError):
                        raise LLMRequestError("content_invalid") from None
        except httpx2.TimeoutException:
            code = "timeout"
        except httpx2.HTTPError:
            code = "transport_error"
        except LLMRequestError as error:
            code = error.code
        except Exception:
            code = "transport_error"
        try:
            usage_service.settle_call(
                self._sessions,
                identity,
                usage=usage,
                status="failed" if code is not None else "succeeded",
                error_code=code,
                output_hash=output_hash,
            )
        except Exception:
            raise LLMRequestError("usage_store_error") from None
        if code is not None:
            raise LLMRequestError(code) from None
        return result
