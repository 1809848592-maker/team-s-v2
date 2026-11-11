"""M3-01 独立审核状态与只追加转换历史；历史交付不补造审核过程。

Revision ID: 0013_delivery_states
Revises: 0012_draft_confirmation
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0013_delivery_states"
down_revision: str | None = "0012_draft_confirmation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_workflows",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('ai_reviewing', 'ai_review_partial_failure', 'returned', "
            "'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_workflows_state",
        ),
        sa.CheckConstraint("revision > 0", name="ck_delivery_workflows_revision"),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_table(
        "delivery_state_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("revision > 0", name="ck_delivery_state_transitions_revision"),
        sa.CheckConstraint(
            "to_state IN ('ai_reviewing', 'ai_review_partial_failure', 'returned', "
            "'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_state_transitions_to_state",
        ),
        sa.CheckConstraint(
            "from_state IS NULL OR from_state IN ('ai_reviewing', 'ai_review_partial_failure', "
            "'returned', 'pending_final_review', 'adopted', 'final_returned')",
            name="ck_delivery_state_transitions_from_state",
        ),
        sa.ForeignKeyConstraint(["delivery_id"], ["delivery_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id", "revision", name="uq_delivery_state_transitions_revision"
        ),
    )
    op.create_index(
        "ix_delivery_state_transitions_delivery_id", "delivery_state_transitions", ["delivery_id"]
    )
    op.execute(
        "CREATE FUNCTION reject_delivery_state_transitions_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER delivery_state_transitions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON delivery_state_transitions FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_delivery_state_transitions_mutation()"
    )


def downgrade() -> None:
    op.drop_table("delivery_state_transitions")
    op.execute("DROP FUNCTION reject_delivery_state_transitions_mutation()")
    op.drop_table("delivery_workflows")
