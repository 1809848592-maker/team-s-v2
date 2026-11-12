import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.plans import PlanContent


def content_hash(content: PlanContent) -> str:
    return hashlib.sha256(
        json.dumps(content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class PlanSelfCheckGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    disclosure_version: str = Field(min_length=1, max_length=80)
    content: PlanContent

    @field_validator("content")
    @classmethod
    def bounded_draft(cls, value):
        encoded = json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
        if len(encoded.encode()) > 480 * 1024:
            raise ValueError("方案草稿超过自检输入上限")

        def check(item):
            if isinstance(item, str) and any(ord(c) < 32 and c not in "\n\r\t" for c in item):
                raise ValueError("文字不能包含控制字符")
            if isinstance(item, dict):
                for child in item.values():
                    check(child)
            if isinstance(item, list):
                for child in item:
                    check(child)

        check(value.model_dump(mode="json"))
        if len({module.id for module in value.modules}) != len(value.modules):
            raise ValueError("分工模块的标识不能重复")
        return value


class SelfCheckProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: Literal["plan_self_check"] = "plan_self_check"
    instruction_version: str
    model: str | None
    input_hash: str | None


class SelfCheckFinding(SelfCheckProvenance):
    check_key: Literal["dependency", "omission", "overlap", "alignment"]
    severity: Literal["important", "suggestion"]
    segment_id: str
    module_id: UUID | None
    location: str
    problem: str
    excerpt: str
    excerpt_verified: bool
    context_key: str | None
    context_excerpt: str | None
    context_excerpt_verified: bool | None
    suggestion: str


class SelfCheckCheck(BaseModel):
    key: Literal["dependency", "omission", "overlap", "alignment"]
    status: Literal["issues", "checked", "uncovered"]
    reason: str


class PlanSelfCheckResult(SelfCheckProvenance):
    request_id: UUID
    content_hash: str
    status: Literal["completed", "incomplete"]
    findings: list[SelfCheckFinding]
    checks: list[SelfCheckCheck]
    coverage_notes: list[str]
    discarded_findings: int = Field(ge=0)
