"""上游绑定快照与旧报告结构；14.9新模型响应使用共享compact契约。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.review.reviewers.schemas import (
    Hash,
    Label,
    OpinionDraft,
    Positive,
    ReviewOutput,
    ReviewSchema,
    ShortText,
    Text,
)


class UpstreamSnapshot(ReviewSchema):
    project_id: UUID
    module_id: UUID
    delivery_id: UUID
    version: Positive
    plan_version_id: UUID
    spec_version_id: UUID
    sha256: Hash
    state: Literal["adopted"]
    workflow_revision: Positive
    handoff_note: str = Field(max_length=10000)


class ReasoningCheck(ReviewSchema):
    key: Label
    status: Literal["checked", "issues", "uncovered"]
    reason: ShortText


class ContextReference(ReviewSchema):
    key: Label
    excerpt: Text


class ReasoningFindingLink(ReviewSchema):
    finding_index: int = Field(strict=True, ge=0, le=19)
    check_key: Label
    context_refs: tuple[ContextReference, ...] = Field(max_length=10)


class ReasoningOutput(ReviewOutput):
    """保留既有报告的冻结结构，不再用来要求模型输出跨数组引用。"""

    findings: tuple[OpinionDraft, ...] = Field(max_length=20)
    checks: tuple[ReasoningCheck, ...] = Field(max_length=601)
    finding_links: tuple[ReasoningFindingLink, ...] = Field(max_length=20)
