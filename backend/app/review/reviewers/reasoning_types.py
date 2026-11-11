"""逻辑与衔接意见的逐项覆盖及独立上下文证据。"""

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
    findings: tuple[OpinionDraft, ...] = Field(max_length=20)
    checks: tuple[ReasoningCheck, ...] = Field(max_length=601)
    finding_links: tuple[ReasoningFindingLink, ...] = Field(max_length=20)
