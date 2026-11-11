"""人的终审决定及实际依据只追加，不能改写旧结论。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class FinalReviewDecision(Base):
    __tablename__ = "final_review_decisions"
    __table_args__ = (
        CheckConstraint("workflow_revision > 0", name="ck_final_review_decisions_revision"),
        CheckConstraint(
            "requested_decision IN ('approve', 'return')",
            name="ck_final_review_decisions_requested",
        ),
        CheckConstraint(
            "outcome IN ('adopted', 'final_returned')", name="ck_final_review_decisions_outcome"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), unique=True
    )
    summary_id: Mapped[UUID] = mapped_column(ForeignKey("review_summaries.id", ondelete="RESTRICT"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    workflow_revision: Mapped[int]
    requested_decision: Mapped[str] = mapped_column(String(8))
    outcome: Mapped[str] = mapped_column(String(16))
    self_review: Mapped[bool]
    context_hash: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str] = mapped_column(String(4000))
    spec_rulings: Mapped[list] = mapped_column(JSONB)
    snapshot: Mapped[dict] = mapped_column(JSONB)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


protect_snapshot(FinalReviewDecision)
