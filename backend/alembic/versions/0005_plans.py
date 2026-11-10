"""M1-05 结构化整体方案的不可变发布版本。

Revision ID: 0005_plans
Revises: 0004_events
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_plans"
down_revision: str | None = "0004_events"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_note", sa.String(2000), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version > 0", name="ck_plan_versions_version_positive"),
        sa.UniqueConstraint("project_id", "version", name="uq_plan_versions_project_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plan_versions_project_id", "plan_versions", ["project_id"])
    op.execute(
        "CREATE FUNCTION reject_plan_versions_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER plan_versions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON plan_versions FOR EACH STATEMENT EXECUTE FUNCTION reject_plan_versions_mutation()"
    )


def downgrade() -> None:
    op.drop_table("plan_versions")
    op.execute("DROP FUNCTION reject_plan_versions_mutation()")
