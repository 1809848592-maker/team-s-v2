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


class DeliveryVersion(Base):
    __tablename__ = "delivery_versions"
    __table_args__ = (
        UniqueConstraint("module_id", "version", name="uq_delivery_versions_module_version"),
        CheckConstraint("version > 0", name="ck_delivery_versions_version_positive"),
        CheckConstraint("size >= 0", name="ck_delivery_versions_size_nonnegative"),
        CheckConstraint(
            "ai_participation IN ('none', 'assisted', 'primary')",
            name="ck_delivery_versions_ai_participation",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    module_id: Mapped[UUID] = mapped_column(
        ForeignKey("modules.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    submitted_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    plan_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    spec_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("spec_versions.id", ondelete="RESTRICT")
    )
    ai_participation: Mapped[str] = mapped_column(String(16))
    ai_note: Mapped[str] = mapped_column(String(2000))
    handoff_note: Mapped[str] = mapped_column(String(10000))


class SharedMaterial(Base):
    __tablename__ = "shared_materials"
    __table_args__ = (CheckConstraint("size >= 0", name="ck_shared_materials_size_nonnegative"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    uploaded_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


protect_snapshot(DeliveryVersion)
protect_snapshot(SharedMaterial)
