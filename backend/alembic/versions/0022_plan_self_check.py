"""M4-02 immutable advisory plan self-check sources and shared review quota.

Revision ID: 0022_plan_self_check
Revises: 0021_project_linkage
"""

import sqlalchemy as sa

from alembic import op

revision = "0022_plan_self_check"
down_revision = "0021_project_linkage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "plan_self_check_requests",
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
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("disclosure_version", sa.String(80), nullable=False),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_plan_self_check_requests_project_id", "plan_self_check_requests", ["project_id"]
    )
    op.execute(
        "CREATE FUNCTION reject_plan_self_check_requests_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'published snapshots are immutable' USING ERRCODE='55000'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER plan_self_check_requests_immutable BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON plan_self_check_requests FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_plan_self_check_requests_mutation()"
    )
    op.add_column("llm_review_admissions", sa.Column("plan_self_check_request_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_llm_review_admissions_plan_self_check_request_id",
        "llm_review_admissions",
        "plan_self_check_requests",
        ["plan_self_check_request_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_llm_review_admissions_plan_self_check_request_id",
        "llm_review_admissions",
        ["plan_self_check_request_id"],
    )
    op.drop_constraint("ck_llm_review_admissions_source", "llm_review_admissions", type_="check")
    op.create_check_constraint(
        "ck_llm_review_admissions_source",
        "llm_review_admissions",
        "num_nonnulls(job_id, project_linkage_review_id, plan_self_check_request_id) = 1",
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM plan_self_check_requests)")):
        raise RuntimeError("存在方案自检记录，不能降级并丢弃其来源")
    op.drop_constraint("ck_llm_review_admissions_source", "llm_review_admissions", type_="check")
    op.drop_constraint(
        "uq_llm_review_admissions_plan_self_check_request_id",
        "llm_review_admissions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_llm_review_admissions_plan_self_check_request_id",
        "llm_review_admissions",
        type_="foreignkey",
    )
    op.drop_column("llm_review_admissions", "plan_self_check_request_id")
    op.create_check_constraint(
        "ck_llm_review_admissions_source",
        "llm_review_admissions",
        "num_nonnulls(job_id, project_linkage_review_id) = 1",
    )
    op.drop_table("plan_self_check_requests")
    op.execute("DROP FUNCTION reject_plan_self_check_requests_mutation()")
