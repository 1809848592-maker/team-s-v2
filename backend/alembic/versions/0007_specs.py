"""M1-07 结构化规范的不可变版本和项目模板资源。

Revision ID: 0007_specs
Revises: 0006_modules
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_specs"
down_revision: str | None = "0006_modules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size >= 0", name="ck_resources_size_nonnegative"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_resources_project_id", "resources", ["project_id"])
    op.create_table(
        "spec_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_note", sa.String(2000), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("published_by", sa.Uuid(), nullable=False),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("version > 0", name="ck_spec_versions_version_positive"),
        sa.UniqueConstraint("project_id", "version", name="uq_spec_versions_project_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spec_versions_project_id", "spec_versions", ["project_id"])
    op.execute(
        "CREATE FUNCTION reject_spec_versions_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER spec_versions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON spec_versions FOR EACH STATEMENT EXECUTE FUNCTION reject_spec_versions_mutation()"
    )


def downgrade() -> None:
    op.drop_table("spec_versions")
    op.execute("DROP FUNCTION reject_spec_versions_mutation()")
    op.drop_table("resources")
