"""方案自检只保留不可变请求元数据，不保存或发布编辑区草稿。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class PlanSelfCheckRequest(Base):
    __tablename__ = "plan_self_check_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    content_hash: Mapped[str] = mapped_column(String(64))
    disclosure_version: Mapped[str] = mapped_column(String(80))
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


protect_snapshot(PlanSelfCheckRequest)
