from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class SpecVersion(Base):
    __tablename__ = "spec_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_spec_versions_project_version"),
        CheckConstraint("version > 0", name="ck_spec_versions_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[int]
    change_note: Mapped[str] = mapped_column(String(2000))
    items: Mapped[list[dict]] = mapped_column(JSONB)
    published_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


protect_snapshot(SpecVersion)
