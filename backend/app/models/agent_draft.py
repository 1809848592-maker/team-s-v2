from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class AgentDraft(Base):
    __tablename__ = "agent_drafts"
    __table_args__ = (CheckConstraint("size >= 0", name="ck_agent_drafts_size"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    module_id: Mapped[UUID] = mapped_column(
        ForeignKey("modules.id", ondelete="RESTRICT"), index=True
    )
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    agent_id: Mapped[UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    handoff_note: Mapped[str] = mapped_column(String(10000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def status(self) -> Literal["pending_confirmation"]:
        # 上传快照本身不可变；人类确认将由独立记录关联，不能改写上传内容。
        return "pending_confirmation"


protect_snapshot(AgentDraft)
