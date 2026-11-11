"""审核当前状态独立于不可变交付；转换历史只追加。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class DeliveryWorkflow(Base):
    __tablename__ = "delivery_workflows"
    __table_args__ = (
        CheckConstraint(
            "state IN ('ai_reviewing', 'ai_review_partial_failure', 'returned', "
            "'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_workflows_state",
        ),
        CheckConstraint("revision > 0", name="ck_delivery_workflows_revision"),
    )

    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryStateTransition(Base):
    __tablename__ = "delivery_state_transitions"
    __table_args__ = (
        UniqueConstraint("delivery_id", "revision", name="uq_delivery_state_transitions_revision"),
        CheckConstraint("revision > 0", name="ck_delivery_state_transitions_revision"),
        CheckConstraint(
            "to_state IN ('ai_reviewing', 'ai_review_partial_failure', 'returned', "
            "'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_state_transitions_to_state",
        ),
        CheckConstraint(
            "from_state IS NULL OR from_state IN ('ai_reviewing', 'ai_review_partial_failure', "
            "'returned', 'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_state_transitions_from_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int]
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


protect_snapshot(DeliveryStateTransition)
