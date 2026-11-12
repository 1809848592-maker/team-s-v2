"""参考文献抽取和引用对应的严格模型输出，不接受模型判定文献存在性。"""

from typing import Literal

from pydantic import Field

from app.review.reviewers.contract import CompactReviewOutput
from app.review.reviewers.schemas import (
    Label,
    OpinionLocation,
    ReviewSchema,
    ShortText,
    Text,
)


class CitationExcerpt(ReviewSchema):
    location: OpinionLocation
    excerpt: Text


class ExtractedReference(ReviewSchema):
    reference_id: Label
    excerpt_verified: bool = True
    evidence: tuple[CitationExcerpt, ...] = Field(min_length=1, max_length=20)
    title: ShortText | None = None
    authors: tuple[ShortText, ...] = Field(default=(), max_length=20)
    year: int | None = Field(default=None, ge=1000, le=3000, strict=True)
    doi: ShortText | None = None


class CitationExtractionOutput(CompactReviewOutput):
    """只标出引用条目，元数据由程序保守解析。"""


class CitationCorrespondence(ReviewSchema):
    reference_id: Label
    status: Literal["matched", "uncited", "uncovered"]
    reason: ShortText
    citations: tuple[CitationExcerpt, ...] = Field(default=(), max_length=20)


class CitationIssueLink(ReviewSchema):
    finding_index: int = Field(strict=True, ge=0, le=99)
    kind: Literal["missing_reference", "uncited_reference", "support_insufficient"]
    reference_id: Label | None = None


class CitationAlignmentOutput(CompactReviewOutput):
    """引用对应的精简模型意见。"""
