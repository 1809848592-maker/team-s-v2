"""M3-12 不改变采用状态的负责人重交要求。

Revision ID: 0018_version_binding
Revises: 0017_final_review
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_version_binding"
down_revision = "0017_final_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "delivery_resubmission_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "delivery_id",
            sa.Uuid(),
            sa.ForeignKey("delivery_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_plan_version_id",
            sa.Uuid(),
            sa.ForeignKey("plan_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_spec_version_id",
            sa.Uuid(),
            sa.ForeignKey("spec_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "target_plan_version_id",
            "target_spec_version_id",
            name="uq_resubmission_delivery_target",
        ),
    )
    op.create_index(
        "ix_delivery_resubmission_requests_project_id",
        "delivery_resubmission_requests",
        ["project_id"],
    )
    op.create_index(
        "ix_delivery_resubmission_requests_delivery_id",
        "delivery_resubmission_requests",
        ["delivery_id"],
    )
    op.execute(
        "CREATE FUNCTION reject_delivery_resubmission_requests_mutation() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER delivery_resubmission_requests_immutable "
        "BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON delivery_resubmission_requests FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_delivery_resubmission_requests_mutation()"
    )


def downgrade():
    op.drop_table("delivery_resubmission_requests")
    op.execute("DROP FUNCTION reject_delivery_resubmission_requests_mutation()")
