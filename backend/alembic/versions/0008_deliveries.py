"""M1-08 不可变交付版本和独立共享材料。

Revision ID: 0008_deliveries
Revises: 0007_specs
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0008_deliveries"
down_revision: str | None = "0007_specs"
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
        "delivery_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("module_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("submitted_by", sa.Uuid(), nullable=False),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("spec_version_id", sa.Uuid(), nullable=False),
        sa.Column("ai_participation", sa.String(16), nullable=False),
        sa.Column("ai_note", sa.String(2000), nullable=False),
        sa.Column("handoff_note", sa.String(10000), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_delivery_versions_version_positive"),
        sa.CheckConstraint("size >= 0", name="ck_delivery_versions_size_nonnegative"),
        sa.CheckConstraint(
            "ai_participation IN ('none', 'assisted', 'primary')",
            name="ck_delivery_versions_ai_participation",
        ),
        sa.UniqueConstraint("module_id", "version", name="uq_delivery_versions_module_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["module_id"], ["modules.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submitted_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_version_id"], ["plan_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["spec_version_id"], ["spec_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delivery_versions_project_id", "delivery_versions", ["project_id"])
    op.create_index("ix_delivery_versions_module_id", "delivery_versions", ["module_id"])
    _protect("delivery_versions")
    op.create_table(
        "shared_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("size >= 0", name="ck_shared_materials_size_nonnegative"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shared_materials_project_id", "shared_materials", ["project_id"])
    _protect("shared_materials")


def downgrade() -> None:
    op.drop_table("shared_materials")
    op.execute("DROP FUNCTION reject_shared_materials_mutation()")
    op.drop_table("delivery_versions")
    op.execute("DROP FUNCTION reject_delivery_versions_mutation()")
