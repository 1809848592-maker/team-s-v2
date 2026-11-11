"""交付生命周期；正式提交状态与审核状态分别表示，私稿不属于正式版本。"""

from enum import StrEnum


class ReviewState(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    AI_REVIEWING = "ai_reviewing"
    AI_REVIEW_PARTIAL_FAILURE = "ai_review_partial_failure"
    RETURNED = "returned"
    PENDING_FINAL_REVIEW = "pending_final_review"
    ADOPTED = "adopted"
    FINAL_RETURNED = "final_returned"
