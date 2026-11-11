"""M3-10 审核报告、汇总、意见及追加式人类处理记录。

Revision ID: 0016_review_reports
Revises: 0015_llm_usage
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0016_review_reports"
down_revision = "0015_llm_usage"
branch_labels = None
depends_on = None


def _created():
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade():
    op.create_table(
        "review_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.Uuid(),
            sa.ForeignKey("delivery_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("review_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("components", JSONB(), nullable=False),
        _created(),
        sa.UniqueConstraint("job_id", "attempt", name="uq_review_reports_attempt"),
        sa.CheckConstraint(
            "attempt > 0 AND workflow_revision > 0", name="ck_review_reports_revision"
        ),
    )
    op.create_index("ix_review_reports_delivery_id", "review_reports", ["delivery_id"])
    op.create_table(
        "review_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.Uuid(),
            sa.ForeignKey("delivery_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            sa.Uuid(),
            sa.ForeignKey("review_reports.id", ondelete="RESTRICT"),
            unique=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("nonconverged", sa.Boolean(), nullable=False),
        _created(),
        sa.CheckConstraint(
            "status IN ('completed', 'incomplete', 'bypassed')", name="ck_review_summaries_status"
        ),
        sa.CheckConstraint("round > 0", name="ck_review_summaries_round"),
    )
    op.create_index("ix_review_summaries_delivery_id", "review_summaries", ["delivery_id"])
    op.create_table(
        "review_findings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "summary_id",
            sa.Uuid(),
            sa.ForeignKey("review_summaries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("opinion", JSONB(), nullable=False),
        sa.Column("duplicate_handling", JSONB(), nullable=True),
        sa.Column(
            "duplicate_of", sa.Uuid(), sa.ForeignKey("review_findings.id", ondelete="RESTRICT")
        ),
        _created(),
    )
    op.create_index("ix_review_findings_summary_id", "review_findings", ["summary_id"])
    op.create_table(
        "review_annotations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "finding_id",
            sa.Uuid(),
            sa.ForeignKey("review_findings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("handling", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(4000), nullable=False),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        _created(),
        sa.UniqueConstraint("finding_id", "revision", name="uq_review_annotations_revision"),
        sa.CheckConstraint("revision > 0", name="ck_review_annotations_revision"),
        sa.CheckConstraint(
            "handling IN ('adopted', 'not_adopted')", name="ck_review_annotations_handling"
        ),
        sa.CheckConstraint(
            "handling <> 'not_adopted' OR length(trim(reason)) > 0",
            name="ck_review_annotations_reason",
        ),
    )
    op.create_index("ix_review_annotations_finding_id", "review_annotations", ["finding_id"])
    for table in ("review_reports", "review_summaries", "review_findings", "review_annotations"):
        op.execute(
            f"CREATE FUNCTION reject_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
            "USING ERRCODE = '55000'; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_{table}_mutation()"
        )


def downgrade():
    for table in ("review_annotations", "review_findings", "review_summaries", "review_reports"):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION reject_{table}_mutation()")
