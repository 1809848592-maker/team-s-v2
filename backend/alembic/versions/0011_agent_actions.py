"""M2-04 智能体草稿及协作来源。

Revision ID: 0011_agent_actions
Revises: 0010_agents
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0011_agent_actions"
down_revision: str | None = "0010_agents"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 旧的不可变历史无需改写，NULL明确保留为人类来源。
    op.add_column("progress_reports", sa.Column("agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_progress_reports_agent_id",
        "progress_reports",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_progress_reports_agent_id", "progress_reports", ["agent_id"])
    op.add_column("request_messages", sa.Column("agent_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_request_messages_agent_id",
        "request_messages",
        "agents",
        ["agent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_request_messages_agent_id", "request_messages", ["agent_id"])
    op.create_table(
        "agent_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("handoff_note", sa.String(10000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size >= 0", name="ck_agent_drafts_size"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("project_id", "module_id", "owner_id", "agent_id"):
        op.create_index(f"ix_agent_drafts_{column}", "agent_drafts", [column])
    op.execute(
        "CREATE FUNCTION reject_agent_drafts_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER agent_drafts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON agent_drafts FOR EACH STATEMENT EXECUTE FUNCTION reject_agent_drafts_mutation()"
    )


def downgrade() -> None:
    op.drop_table("agent_drafts")
    op.execute("DROP FUNCTION reject_agent_drafts_mutation()")
    for table in ("request_messages", "progress_reports"):
        op.drop_index(f"ix_{table}_agent_id", table_name=table)
        op.drop_constraint(f"fk_{table}_agent_id", table, type_="foreignkey")
        op.drop_column(table, "agent_id")
