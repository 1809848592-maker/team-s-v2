"""负责人启用模型的知情同意与提交人的首次外发告知记录。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class ProjectModelConsent(Base):
    __tablename__ = "project_model_consents"
    __table_args__ = (
        UniqueConstraint("project_id", "disclosure_version", name="uq_project_consent_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    disclosure_version: Mapped[str] = mapped_column(String(80))
    notice: Mapped[dict] = mapped_column(JSONB)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SubmissionDisclosure(Base):
    __tablename__ = "submission_disclosures"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "user_id", "disclosure_version", name="uq_submission_disclosure_user"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    delivery_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_versions.id", ondelete="RESTRICT")
    )
    disclosure_version: Mapped[str] = mapped_column(String(80))
    notice: Mapped[dict] = mapped_column(JSONB)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


protect_snapshot(ProjectModelConsent)
protect_snapshot(SubmissionDisclosure)
