"""模型调用只记录元数据和用量，不持久化Key或请求/响应全文。"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LLMProjectPolicy(Base):
    __tablename__ = "llm_project_policies"
    __table_args__ = (
        CheckConstraint("daily_review_limit >= 0", name="ck_llm_project_policies_limit"),
    )

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True
    )
    daily_review_limit: Mapped[int]
    # 预留项目自带Key引用；当前统一使用平台Key，不读取此字段。
    api_key_reference: Mapped[str | None] = mapped_column(String(128))


class DailyBudget(Base):
    __tablename__ = "llm_daily_budgets"
    __table_args__ = (
        CheckConstraint(
            "review_count >= 0 AND reserved_tokens >= 0 AND charged_tokens >= 0",
            name="ck_llm_daily_budgets_nonnegative",
        ),
    )

    scope_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    review_count: Mapped[int] = mapped_column(default=0)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    charged_tokens: Mapped[int] = mapped_column(BigInteger, default=0)


class ReviewAdmission(Base):
    __tablename__ = "llm_review_admissions"
    __table_args__ = (CheckConstraint("call_count >= 0", name="ck_llm_review_admissions_calls"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_jobs.id", ondelete="RESTRICT"), unique=True
    )
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    day: Mapped[date] = mapped_column(Date)
    call_count: Mapped[int] = mapped_column(default=0)


class ModelCall(Base):
    __tablename__ = "llm_model_calls"
    __table_args__ = (
        CheckConstraint(
            "(admission_id IS NOT NULL AND spec_draft_request_id IS NULL) OR "
            "(admission_id IS NULL AND spec_draft_request_id IS NOT NULL)",
            name="ck_llm_model_calls_source",
        ),
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'unknown')",
            name="ck_llm_model_calls_status",
        ),
        CheckConstraint(
            "reserved_tokens > 0 AND charged_tokens >= 0",
            name="ck_llm_model_calls_budget",
        ),
        CheckConstraint(
            "(prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL) OR "
            "(prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL AND "
            "total_tokens IS NOT NULL AND prompt_tokens >= 0 AND completion_tokens >= 0 AND "
            "total_tokens = prompt_tokens + completion_tokens)",
            name="ck_llm_model_calls_usage",
        ),
        CheckConstraint(
            "(cache_hit_tokens IS NULL AND cache_miss_tokens IS NULL) OR "
            "(cache_hit_tokens IS NOT NULL AND cache_miss_tokens IS NOT NULL AND "
            "prompt_tokens IS NOT NULL AND cache_hit_tokens >= 0 AND cache_miss_tokens >= 0 AND "
            "cache_hit_tokens + cache_miss_tokens = prompt_tokens)",
            name="ck_llm_model_calls_cache",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    admission_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("llm_review_admissions.id", ondelete="RESTRICT")
    )
    spec_draft_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("spec_draft_requests.id", ondelete="RESTRICT"), unique=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    reviewer: Mapped[str] = mapped_column(String(80))
    instruction_version: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    reserved_tokens: Mapped[int] = mapped_column(BigInteger)
    charged_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_hit_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cache_miss_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_cost_cny: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    prices: Mapped[dict] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
