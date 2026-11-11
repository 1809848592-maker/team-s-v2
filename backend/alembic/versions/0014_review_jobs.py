"""M3-02 持久审核任务与租约；不回填历史版本任务。

Revision ID: 0014_review_jobs
Revises: 0013_delivery_states
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_review_jobs"
down_revision: str | None = "0013_delivery_states"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "review_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(32), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
            name="ck_review_jobs_status",
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND max_attempts > 0 AND attempt <= max_attempts",
            name="ck_review_jobs_attempts",
        ),
        sa.CheckConstraint("workflow_revision > 0", name="ck_review_jobs_workflow_revision"),
        sa.CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_review_jobs_lease",
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR last_error IN ('pipeline_not_ready', 'execution_error', "
            "'timeout', 'lease_expired', 'stale_workflow')",
            name="ck_review_jobs_error",
        ),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id"),
    )
    op.create_index("ix_review_jobs_due", "review_jobs", ["status", "available_at"])


def downgrade() -> None:
    op.drop_table("review_jobs")
