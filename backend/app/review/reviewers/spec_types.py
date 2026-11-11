"""规范审核的逐条覆盖；模型的覆盖说明不是人类对规范不适用的裁定。"""

from typing import Literal

from pydantic import Field

from app.review.reviewers.schemas import Label, ReviewOutput, ReviewSchema, ShortText


class SpecCheck(ReviewSchema):
    spec_number: Label
    status: Literal["checked", "issues", "uncovered", "not_applicable"]
    reason: ShortText


class SpecReviewOutput(ReviewOutput):
    checks: tuple[SpecCheck, ...] = Field(max_length=500)
