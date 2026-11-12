"""M3-15 specification drafting requests and independently accounted model calls.

Revision ID: 0020_spec_drafting
Revises: 0019_model_disclosure
"""

import sqlalchemy as sa

from alembic import op

revision = "0020_spec_drafting"
down_revision = "0019_model_disclosure"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "spec_draft_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "requested_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("disclosure_version", sa.String(80), nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_spec_draft_requests_project_id", "spec_draft_requests", ["project_id"])
    op.execute(
        "CREATE FUNCTION reject_spec_draft_requests_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
        "USING ERRCODE = '55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER spec_draft_requests_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON spec_draft_requests FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_spec_draft_requests_mutation()"
    )
    op.alter_column("llm_model_calls", "admission_id", nullable=True)
    op.add_column("llm_model_calls", sa.Column("spec_draft_request_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_llm_model_calls_spec_draft_request_id",
        "llm_model_calls",
        "spec_draft_requests",
        ["spec_draft_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_llm_model_calls_spec_draft_request_id", "llm_model_calls", ["spec_draft_request_id"]
    )
    op.create_check_constraint(
        "ck_llm_model_calls_source",
        "llm_model_calls",
        "(admission_id IS NOT NULL AND spec_draft_request_id IS NULL) OR "
        "(admission_id IS NULL AND spec_draft_request_id IS NOT NULL)",
    )


def downgrade():
    # 有真实起草用量时不能丢弃账本或伪造审核来源。
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM llm_model_calls WHERE spec_draft_request_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("存在规范起草用量记录，不能降级并丢弃其来源")
    op.drop_constraint("ck_llm_model_calls_source", "llm_model_calls", type_="check")
    op.drop_constraint(
        "uq_llm_model_calls_spec_draft_request_id", "llm_model_calls", type_="unique"
    )
    op.drop_constraint(
        "fk_llm_model_calls_spec_draft_request_id", "llm_model_calls", type_="foreignkey"
    )
    op.drop_column("llm_model_calls", "spec_draft_request_id")
    op.alter_column("llm_model_calls", "admission_id", nullable=False)
    op.drop_table("spec_draft_requests")
    op.execute("DROP FUNCTION reject_spec_draft_requests_mutation()")
