"""管理员用量只包含元数据，金额始终以十进制字符串传递。"""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UsageTotals(BaseModel):
    calls: int = 0
    measured_calls: int = 0
    unknown_usage_calls: int = 0
    unknown_cache_calls: int = 0
    unpriced_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    estimated_cost_cny: str = "0.0000000000"


class ProjectUsage(UsageTotals):
    project_id: UUID
    project_name: str


class ReviewerUsage(UsageTotals):
    reviewer: str


class UsageLimits(BaseModel):
    platform_daily_reviews: int
    platform_daily_tokens: int
    project_daily_reviews: int
    max_calls_per_review: int


class UsageReport(BaseModel):
    start: date
    end: date
    timezone: Literal["UTC"] = "UTC"
    totals: UsageTotals
    by_project: list[ProjectUsage]
    by_reviewer: list[ReviewerUsage]
    limits: UsageLimits


class AdminProjectInfo(BaseModel):
    id: UUID
    name: str
    daily_review_limit: int


class ProjectQuotaUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    daily_review_limit: int = Field(strict=True, ge=0, le=2_147_483_647)
