"""数据库持久审核任务；领取凭证只用于内部并发控制，不对外返回。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'paused', 'succeeded', 'failed')",
            name="ck_review_jobs_status",
        ),
        CheckConstraint(
            "attempt >= 0 AND max_attempts > 0 AND attempt <= max_attempts",
            name="ck_review_jobs_attempts",
        ),
        CheckConstraint("workflow_revision > 0", name="ck_review_jobs_workflow_revision"),
        CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_review_jobs_lease",
        ),
        CheckConstraint(
            "last_error IS NULL OR last_error IN ('pipeline_not_ready', 'execution_error', "
            "'timeout', 'lease_expired', 'stale_workflow', 'quota_exceeded')",
            name="ck_review_jobs_error",
        ),
        Index("ix_review_jobs_due", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    workflow_revision: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def next_attempt_at(self) -> datetime | None:
        return self.available_at if self.status in {"retry_wait", "paused"} else None

    @property
    def message(self) -> str:
        if self.status == "failed":
            return {
                "pipeline_not_ready": "审核处理尚未就绪，本次审核未完成，尚无审核结论。",
                "timeout": "审核处理超时，重试次数已用完，本次审核未完成。",
                "lease_expired": "审核处理曾中断，重试次数已用完，本次审核未完成。",
                "stale_workflow": "交付审核状态已变化，此后台任务已停止。",
            }.get(self.last_error, "审核处理失败，重试次数已用完，本次审核未完成。")
        return {
            "queued": "审核任务已排队，尚无审核结论。",
            "running": "正在处理审核任务，尚无审核结论。",
            "retry_wait": "审核处理未完成，正在等待自动重试。",
            "paused": "当日审核额度已用完，审核已暂停，尚无审核结论。",
            "succeeded": "后台处理已完成，请查看审核报告。",
        }[self.status]
