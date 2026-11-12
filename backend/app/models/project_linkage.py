"""项目完整衔接审核使用独立队列；输入和每次执行报告均保留。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.immutable import protect_snapshot


class ProjectLinkageReview(Base):
    __tablename__ = "project_linkage_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial_failed')",
            name="ck_project_linkage_reviews_status",
        ),
        CheckConstraint(
            "attempt >= 0 AND max_attempts > 0 AND attempt <= max_attempts",
            name="ck_project_linkage_reviews_attempts",
        ),
        CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_project_linkage_reviews_lease",
        ),
        Index("ix_project_linkage_reviews_due", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    plan_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    spec_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("spec_versions.id", ondelete="RESTRICT")
    )
    frozen_input: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    claim_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProjectLinkageResult(Base):
    __tablename__ = "project_linkage_results"
    __table_args__ = (
        UniqueConstraint("review_id", "attempt", name="uq_project_linkage_results_attempt"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_linkage_reviews.id", ondelete="RESTRICT")
    )
    claim_token: Mapped[UUID]
    attempt: Mapped[int]
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectLinkageRequest(Base):
    __tablename__ = "project_linkage_requests"

    review_id: Mapped[UUID] = mapped_column(
        ForeignKey("project_linkage_reviews.id", ondelete="RESTRICT"), primary_key=True
    )
    finding_id: Mapped[UUID] = mapped_column(primary_key=True)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("collaboration_requests.id", ondelete="RESTRICT"), unique=True
    )


def _freeze_input(table, connection, **kwargs):
    if connection.dialect.name != "postgresql":
        return
    preparer = connection.dialect.identifier_preparer
    schema = connection.schema_for_object(table) or connection.dialect.default_schema_name
    prefix = preparer.quote(schema) + "." if schema else ""
    function = prefix + "freeze_project_linkage_input"
    target = prefix + "project_linkage_reviews"
    connection.exec_driver_sql(
        f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "IF ROW(OLD.id, OLD.project_id, OLD.requested_by, OLD.plan_version_id, "
        "OLD.spec_version_id, OLD.frozen_input, OLD.created_at) IS DISTINCT FROM "
        "ROW(NEW.id, NEW.project_id, NEW.requested_by, NEW.plan_version_id, "
        "NEW.spec_version_id, NEW.frozen_input, NEW.created_at) THEN "
        "RAISE EXCEPTION 'project linkage inputs are immutable' USING ERRCODE='55000'; "
        "END IF; RETURN NEW; END; $$"
    )
    connection.exec_driver_sql(
        f"CREATE TRIGGER project_linkage_input_immutable BEFORE UPDATE ON {target} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def _drop_freeze(table, connection, **kwargs):
    if connection.dialect.name == "postgresql":
        preparer = connection.dialect.identifier_preparer
        schema = connection.schema_for_object(table) or connection.dialect.default_schema_name
        prefix = preparer.quote(schema) + "." if schema else ""
        connection.exec_driver_sql(
            f"DROP FUNCTION IF EXISTS {prefix}freeze_project_linkage_input()"
        )


event.listen(ProjectLinkageReview.__table__, "after_create", _freeze_input)
event.listen(ProjectLinkageReview.__table__, "after_drop", _drop_freeze)
protect_snapshot(ProjectLinkageResult)
protect_snapshot(ProjectLinkageRequest)
