"""有界且严格的意见结构；模型无权填写审核来源或交付状态。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Positive = Annotated[int, Field(strict=True, ge=1, le=10_000_000)]
Category = Literal["specification", "citation", "logic", "linkage"]
Severity = Literal["blocking", "important", "suggestion"]


class ReviewSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class OpinionLocation(ReviewSchema):
    description: ShortText
    part: ShortText | None = None
    page: Positive | None = None
    slide: Positive | None = None
    paragraph: Positive | None = None
    table: Positive | None = None
    row: Positive | None = None
    column: Positive | None = None
    shape_id: Label | None = None
    line_start: Positive | None = None
    line_end: Positive | None = None

    @model_validator(mode="after")
    def check_lines(self):
        if self.line_end is not None and (
            self.line_start is None or self.line_end < self.line_start
        ):
            raise ValueError("位置行号范围无效")
        return self


class OpinionBasis(ReviewSchema):
    spec_number: Label | None = None
    excerpt: Text | None = None
    excerpt_verified: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    context_key: Label | None = Field(default=None, exclude_if=lambda value: value is None)
    context_excerpt: Text | None = Field(default=None, exclude_if=lambda value: value is None)
    context_excerpt_verified: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def has_basis(self):
        if self.spec_number is None and self.excerpt is None:
            raise ValueError("意见必须引用条目编号或原文片段")
        return self


class OpinionDraft(ReviewSchema):
    category: Category
    severity: Severity
    location: OpinionLocation
    problem: Text
    basis: OpinionBasis
    suggestion: Text
    original_severity: Severity | None = Field(default=None, exclude_if=lambda value: value is None)
    convergence_note: ShortText | None = Field(default=None, exclude_if=lambda value: value is None)


class ReviewCoverage(ReviewSchema):
    checked: tuple[ShortText, ...] = Field(max_length=100)
    uncovered: tuple[ShortText, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def has_coverage(self):
        if not self.checked and not self.uncovered:
            raise ValueError("必须说明已覆盖或未覆盖的范围")
        return self


class ReviewOutput(ReviewSchema):
    findings: tuple[OpinionDraft, ...] = Field(max_length=100)
    coverage: ReviewCoverage


class HistoricalOpinion(ReviewSchema):
    finding_id: UUID
    opinion: OpinionDraft
    handling: Literal["unhandled", "adopted", "not_adopted"]
    reason: str = Field(default="", max_length=4000)
    actor_id: UUID | None = None

    @model_validator(mode="after")
    def rejected_reason(self):
        if self.handling == "not_adopted" and not self.reason.strip():
            raise ValueError("不采纳记录必须有理由")
        return self


Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
ModelName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class RecordedOpinion(OpinionDraft):
    finding_id: UUID
    reviewer: Label
    instruction_version: Label
    model: ModelName
    input_hash: Hash
    instruction_hash: Hash
    call_id: UUID


class ReviewerAttempt(ReviewSchema):
    call_id: UUID
    input_hash: Hash
    outcome: Literal["completed", "invalid_structure", "failed"]
    error_code: Label | None = None


class ReviewerResult(ReviewSchema):
    status: Literal["completed", "incomplete"]
    reviewer: Label
    instruction_version: Label
    model: ModelName
    instruction_hash: Hash
    context_hash: Hash | None
    findings: tuple[RecordedOpinion, ...]
    coverage: ReviewCoverage | None
    attempts: tuple[ReviewerAttempt, ...] = Field(max_length=2)
    error_code: Label | None = None

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")
