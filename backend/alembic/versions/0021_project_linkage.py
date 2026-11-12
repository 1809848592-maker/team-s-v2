"""M4-01 project linkage review queue, frozen inputs, reports and usage source.

Revision ID: 0021_project_linkage
Revises: 0020_spec_drafting
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021_project_linkage"
down_revision = "0020_spec_drafting"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_linkage_reviews",
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
        sa.Column(
            "plan_version_id",
            sa.Uuid(),
            sa.ForeignKey("plan_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "spec_version_id",
            sa.Uuid(),
            sa.ForeignKey("spec_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("frozen_input", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error_code", sa.String(40)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'partial_failed')",
            name="ck_project_linkage_reviews_status",
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND max_attempts > 0 AND attempt <= max_attempts",
            name="ck_project_linkage_reviews_attempts",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL) OR "
            "(status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)",
            name="ck_project_linkage_reviews_lease",
        ),
    )
    op.create_index(
        "ix_project_linkage_reviews_project_id", "project_linkage_reviews", ["project_id"]
    )
    op.create_index(
        "ix_project_linkage_reviews_due", "project_linkage_reviews", ["status", "available_at"]
    )
    op.create_table(
        "project_linkage_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "review_id",
            sa.Uuid(),
            sa.ForeignKey("project_linkage_reviews.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("claim_token", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("review_id", "attempt", name="uq_project_linkage_results_attempt"),
    )
    op.create_table(
        "project_linkage_requests",
        sa.Column(
            "review_id",
            sa.Uuid(),
            sa.ForeignKey("project_linkage_reviews.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("finding_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("collaboration_requests.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
    )
    op.execute(
        "CREATE FUNCTION freeze_project_linkage_input() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF ROW(OLD.id, OLD.project_id, OLD.requested_by, OLD.plan_version_id, "
        "OLD.spec_version_id, OLD.frozen_input, OLD.created_at) "
        "IS DISTINCT FROM ROW(NEW.id, NEW.project_id, NEW.requested_by, NEW.plan_version_id, "
        "NEW.spec_version_id, NEW.frozen_input, NEW.created_at) "
        "THEN RAISE EXCEPTION 'project linkage inputs are immutable' "
        "USING ERRCODE='55000'; END IF; RETURN NEW; END; $$"
    )
    op.execute(
        "CREATE TRIGGER project_linkage_input_immutable BEFORE UPDATE ON project_linkage_reviews "
        "FOR EACH ROW EXECUTE FUNCTION freeze_project_linkage_input()"
    )
    for name in ("project_linkage_results", "project_linkage_requests"):
        op.execute(
            f"CREATE FUNCTION reject_{name}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
            "USING ERRCODE='55000'; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {name} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_{name}_mutation()"
        )
    op.alter_column("llm_review_admissions", "job_id", nullable=True)
    op.add_column("llm_review_admissions", sa.Column("project_linkage_review_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_llm_review_admissions_project_linkage_review_id",
        "llm_review_admissions",
        "project_linkage_reviews",
        ["project_linkage_review_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_llm_review_admissions_project_linkage_review_id",
        "llm_review_admissions",
        ["project_linkage_review_id"],
    )
    op.create_check_constraint(
        "ck_llm_review_admissions_source",
        "llm_review_admissions",
        "(job_id IS NOT NULL AND project_linkage_review_id IS NULL) OR "
        "(job_id IS NULL AND project_linkage_review_id IS NOT NULL)",
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM project_linkage_reviews)")):
        raise RuntimeError("存在项目衔接审核记录，不能降级并丢弃其来源")
    op.drop_constraint("ck_llm_review_admissions_source", "llm_review_admissions", type_="check")
    op.drop_constraint(
        "uq_llm_review_admissions_project_linkage_review_id",
        "llm_review_admissions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_llm_review_admissions_project_linkage_review_id",
        "llm_review_admissions",
        type_="foreignkey",
    )
    op.drop_column("llm_review_admissions", "project_linkage_review_id")
    op.alter_column("llm_review_admissions", "job_id", nullable=False)
    for name in ("project_linkage_requests", "project_linkage_results"):
        op.drop_table(name)
        op.execute(f"DROP FUNCTION reject_{name}_mutation()")
    op.drop_table("project_linkage_reviews")
    op.execute("DROP FUNCTION freeze_project_linkage_input()")
