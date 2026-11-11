"""审核报告与意见为不可变快照；人的处理记录只追加。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class ReviewReport(Base):
    __tablename__ = "review_reports"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_review_reports_attempt"),
        CheckConstraint("attempt > 0 AND workflow_revision > 0", name="ck_review_reports_revision"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), index=True
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("review_jobs.id", ondelete="RESTRICT"))
    attempt: Mapped[int]
    workflow_revision: Mapped[int]
    components: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewSummary(Base):
    __tablename__ = "review_summaries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed', 'incomplete', 'bypassed')", name="ck_review_summaries_status"
        ),
        CheckConstraint("round > 0", name="ck_review_summaries_round"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), index=True
    )
    report_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("review_reports.id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(16))
    round: Mapped[int]
    nonconverged: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewFinding(Base):
    __tablename__ = "review_findings"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    summary_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_summaries.id", ondelete="RESTRICT"), index=True
    )
    opinion: Mapped[dict] = mapped_column(JSONB)
    duplicate_of: Mapped[UUID | None] = mapped_column(
        ForeignKey("review_findings.id", ondelete="RESTRICT")
    )
    duplicate_handling: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewAnnotation(Base):
    __tablename__ = "review_annotations"
    __table_args__ = (
        UniqueConstraint("finding_id", "revision", name="uq_review_annotations_revision"),
        CheckConstraint("revision > 0", name="ck_review_annotations_revision"),
        CheckConstraint(
            "handling IN ('adopted', 'not_adopted')", name="ck_review_annotations_handling"
        ),
        CheckConstraint(
            "handling <> 'not_adopted' OR length(trim(reason)) > 0",
            name="ck_review_annotations_reason",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("review_findings.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int]
    handling: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(4000))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


for model in (ReviewReport, ReviewSummary, ReviewFinding, ReviewAnnotation):
    protect_snapshot(model)
