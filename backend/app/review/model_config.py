"""只含模型选择与价格快照的配置；密钥由调用方另行传入。"""

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
)


class LLMConfigurationError(ValueError):
    def __init__(self) -> None:
        super().__init__("模型配置不可用，请检查平台配置。")


class _FrozenConfiguration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class PriceSnapshot(_FrozenConfiguration):
    currency: Literal["CNY"] = "CNY"
    input_cache_hit: Decimal = Field(ge=0, allow_inf_nan=False)
    input_cache_miss: Decimal = Field(ge=0, allow_inf_nan=False)
    output: Decimal = Field(ge=0, allow_inf_nan=False)
    effective_date: date
    estimate_basis: Literal["peak_ceiling"] = "peak_ceiling"


class ReviewerModel(_FrozenConfiguration):
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:/-]+$")
    base_url: str
    key_env: str = Field(default="DEEPSEEK_API_KEY", pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    thinking: bool = Field(strict=True)
    max_tokens: int = Field(ge=1, le=65536, strict=True)
    timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    prices: PriceSnapshot

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            if any(ord(character) <= 32 for character in value) or "\\" in value:
                raise ValueError
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "?" in value
                or "#" in value
            ):
                raise ValueError
            parsed.port
            if parsed.scheme == "http" and parsed.hostname != "localhost":
                if not ipaddress.ip_address(parsed.hostname).is_loopback:
                    raise ValueError
        except (ValueError, TypeError):
            raise ValueError("服务地址必须为无凭据、查询参数或片段的HTTPS地址") from None
        return value.rstrip("/")

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"


class ModelConfiguration(_FrozenConfiguration):
    provider: Literal["deepseek"] = "deepseek"
    reviewers: Mapping[str, ReviewerModel]

    @field_validator("reviewers")
    @classmethod
    def validate_reviewers(cls, value: Mapping[str, ReviewerModel]) -> Mapping[str, ReviewerModel]:
        if not value or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in value):
            raise ValueError("必须配置具名审核员")
        return MappingProxyType(dict(value))

    @field_serializer("reviewers")
    def serialize_reviewers(self, value: Mapping[str, ReviewerModel]) -> dict:
        return {name: reviewer.model_dump(mode="json") for name, reviewer in value.items()}


def load_model_config(path: str | Path | None = None) -> ModelConfiguration:
    source = Path(path) if path is not None else Path(__file__).with_name("models.toml")
    try:
        with source.open("rb") as stream:
            return ModelConfiguration.model_validate(tomllib.load(stream))
    except (OSError, ValueError, TypeError, ValidationError):
        raise LLMConfigurationError from None
