"""M1-04 只追加事件日志。

Revision ID: 0004_events
Revises: 0003_projects
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_events"
down_revision: str | None = "0003_projects"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(10), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("represented_user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("object_type", sa.String(80), nullable=False),
        sa.Column("object_id", sa.String(256), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=True),
        sa.Column("spec_version_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("actor_type IN ('human', 'agent')", name="ck_events_actor_type"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for field in ("project_id", "actor_id", "represented_user_id", "object_id", "task_id"):
        op.create_index(f"ix_events_{field}", "events", [field])
    op.execute(
        "CREATE FUNCTION reject_events_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'events are append-only' USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON events "
        "FOR EACH STATEMENT EXECUTE FUNCTION reject_events_mutation()"
    )


def downgrade() -> None:
    op.drop_table("events")
    op.execute("DROP FUNCTION reject_events_mutation()")
