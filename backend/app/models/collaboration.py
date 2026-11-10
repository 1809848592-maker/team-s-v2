from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.agent import Agent
from app.models.base import Base
from app.models.immutable import protect_snapshot


class ProgressReport(Base):
    __tablename__ = "progress_reports"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('not_started', 'planning', 'writing', 'checking')",
            name="ck_progress_reports_stage",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    module_id: Mapped[UUID] = mapped_column(
        ForeignKey("modules.id", ondelete="RESTRICT"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), index=True
    )
    agent: Mapped[Agent | None] = relationship(lazy="joined")
    stage: Mapped[str] = mapped_column(String(20))
    summary: Mapped[str] = mapped_column(String(1000))
    plan_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def source(self) -> Literal["human", "agent"]:
        return "agent" if self.agent_id is not None else "human"

    @property
    def agent_name(self) -> str | None:
        return self.agent.name if self.agent is not None else None


class CollaborationRequest(Base):
    __tablename__ = "collaboration_requests"
    __table_args__ = (
        CheckConstraint(
            "(target_user_id IS NOT NULL AND target_module_id IS NULL) OR "
            "(target_user_id IS NULL AND target_module_id IS NOT NULL)",
            name="ck_collaboration_requests_target",
        ),
        CheckConstraint(
            "status IN ('open', 'accepted', 'responded', 'resolved')",
            name="ck_collaboration_requests_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(String(10000))
    target_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    target_module_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("modules.id", ondelete="RESTRICT")
    )
    expected_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")
    accepted_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestMessage(Base):
    __tablename__ = "request_messages"
    __table_args__ = (
        CheckConstraint("kind IN ('response', 'supplement')", name="ck_request_messages_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collaboration_requests.id", ondelete="RESTRICT"), index=True
    )
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    agent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), index=True
    )
    agent: Mapped[Agent | None] = relationship(lazy="joined")
    kind: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(String(10000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def source(self) -> Literal["human", "agent"]:
        return "agent" if self.agent_id is not None else "human"

    @property
    def agent_name(self) -> str | None:
        return self.agent.name if self.agent is not None else None


protect_snapshot(ProgressReport)
protect_snapshot(RequestMessage)
