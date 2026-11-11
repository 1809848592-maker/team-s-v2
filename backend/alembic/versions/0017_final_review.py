"""M3-11 不可变终审结论与上下文快照。

Revision ID: 0017_final_review
Revises: 0016_review_reports
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0017_final_review"
down_revision = "0016_review_reports"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "final_review_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.Uuid(),
            sa.ForeignKey("delivery_versions.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "summary_id",
            sa.Uuid(),
            sa.ForeignKey("review_summaries.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("workflow_revision", sa.Integer(), nullable=False),
        sa.Column("requested_decision", sa.String(8), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("self_review", sa.Boolean(), nullable=False),
        sa.Column("context_hash", sa.String(64), nullable=False),
        sa.Column("comment", sa.String(4000), nullable=False),
        sa.Column("spec_rulings", JSONB(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("workflow_revision > 0", name="ck_final_review_decisions_revision"),
        sa.CheckConstraint(
            "requested_decision IN ('approve', 'return')",
            name="ck_final_review_decisions_requested",
        ),
        sa.CheckConstraint(
            "outcome IN ('adopted', 'final_returned')", name="ck_final_review_decisions_outcome"
        ),
    )
    op.execute(
        "CREATE FUNCTION reject_final_review_decisions_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER final_review_decisions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON final_review_decisions FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_final_review_decisions_mutation()"
    )


def downgrade():
    op.drop_table("final_review_decisions")
    op.execute("DROP FUNCTION reject_final_review_decisions_mutation()")
