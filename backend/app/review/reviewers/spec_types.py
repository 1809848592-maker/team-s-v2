"""规范审核的逐条覆盖；模型的覆盖说明不是人类对规范不适用的裁定。"""

from typing import Literal

from app.review.reviewers.contract import CompactReviewOutput
from app.review.reviewers.schemas import Label, ReviewSchema, ShortText


class SpecCheck(ReviewSchema):
    spec_number: Label
    status: Literal["checked", "issues", "uncovered", "not_applicable"]
    reason: ShortText


class SpecReviewOutput(CompactReviewOutput):
    """模型仅返回意见；逐条规范状态由平台推导。"""
