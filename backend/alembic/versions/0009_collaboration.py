"""M1-09 进度回报及协作需求闭环。

Revision ID: 0009_collaboration
Revises: 0008_deliveries
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0009_collaboration"
down_revision: str | None = "0008_deliveries"
branch_labels: str | None = None
depends_on: str | None = None


def _protect(table_name: str) -> None:
    op.execute(
        f"CREATE FUNCTION reject_{table_name}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        f"CREATE TRIGGER {table_name}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        f"ON {table_name} FOR EACH STATEMENT EXECUTE FUNCTION reject_{table_name}_mutation()"
    )


def upgrade() -> None:
    op.create_table(
        "progress_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "stage IN ('not_started', 'planning', 'writing', 'checking')",
            name="ck_progress_reports_stage",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_progress_reports_project_id", "progress_reports", ["project_id"])
    op.create_index("ix_progress_reports_module_id", "progress_reports", ["module_id"])
    _protect("progress_reports")
    op.create_table(
        "collaboration_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.String(10000), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_module_id", sa.Uuid(), nullable=True),
        sa.Column("expected_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("accepted_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(target_user_id IS NOT NULL AND target_module_id IS NULL) OR "
            "(target_user_id IS NULL AND target_module_id IS NOT NULL)",
            name="ck_collaboration_requests_target",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'accepted', 'responded', 'resolved')",
            name="ck_collaboration_requests_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_module_id"], ["modules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_collaboration_requests_project_id", "collaboration_requests", ["project_id"]
    )
    op.create_table(
        "request_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.String(10000), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('response', 'supplement')", name="ck_request_messages_kind"),
        sa.ForeignKeyConstraint(["request_id"], ["collaboration_requests.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_request_messages_request_id", "request_messages", ["request_id"])
    _protect("request_messages")


def downgrade() -> None:
    op.drop_table("request_messages")
    op.execute("DROP FUNCTION reject_request_messages_mutation()")
    op.drop_table("collaboration_requests")
    op.drop_table("progress_reports")
    op.execute("DROP FUNCTION reject_progress_reports_mutation()")
