"""负责人要求依新版重交的只追加记录；不改变原交付的采用状态。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class DeliveryResubmissionRequest(Base):
    __tablename__ = "delivery_resubmission_requests"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "target_plan_version_id",
            "target_spec_version_id",
            name="uq_resubmission_delivery_target",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_plan_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    target_spec_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("spec_versions.id", ondelete="RESTRICT")
    )
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


protect_snapshot(DeliveryResubmissionRequest)
