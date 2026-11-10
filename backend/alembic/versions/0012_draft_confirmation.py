"""M2-06 本人确认智能体草稿，确认记录独立不可变。

Revision ID: 0012_draft_confirmation
Revises: 0011_agent_actions
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0012_draft_confirmation"
down_revision: str | None = "0011_agent_actions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "draft_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("confirmed_by", sa.Uuid(), nullable=False),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["agent_drafts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("draft_id", name="uq_draft_confirmations_draft_id"),
        sa.UniqueConstraint("delivery_id", name="uq_draft_confirmations_delivery_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_draft_confirmations_confirmed_by", "draft_confirmations", ["confirmed_by"])
    op.execute(
        "CREATE FUNCTION reject_draft_confirmations_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER draft_confirmations_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON draft_confirmations FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_draft_confirmations_mutation()"
    )


def downgrade() -> None:
    op.drop_table("draft_confirmations")
    op.execute("DROP FUNCTION reject_draft_confirmations_mutation()")
