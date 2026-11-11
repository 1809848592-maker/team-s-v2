"""M3-03 模型用量、审核名额和额度暂停。

Revision ID: 0015_llm_usage
Revises: 0014_review_jobs
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0015_llm_usage"
down_revision: str | None = "0014_review_jobs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "llm_project_policies",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("daily_review_limit", sa.Integer(), nullable=False),
        sa.Column("api_key_reference", sa.String(128), nullable=True),
        sa.CheckConstraint("daily_review_limit >= 0", name="ck_llm_project_policies_limit"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "llm_daily_budgets",
        sa.Column("scope_key", sa.String(64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "review_count >= 0 AND reserved_tokens >= 0 AND charged_tokens >= 0",
            name="ck_llm_daily_budgets_nonnegative",
        ),
        sa.PrimaryKeyConstraint("scope_key", "day"),
    )
    op.create_table(
        "llm_review_admissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("call_count >= 0", name="ck_llm_review_admissions_calls"),
        sa.ForeignKeyConstraint(["job_id"], ["review_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_table(
        "llm_model_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("admission_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("reviewer", sa.String(80), nullable=False),
        sa.Column("instruction_version", sa.String(80), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("charged_tokens", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_hit_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_miss_tokens", sa.BigInteger(), nullable=True),
        sa.Column("estimated_cost_cny", sa.Numeric(20, 10), nullable=True),
        sa.Column("prices", JSONB(), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'unknown')",
            name="ck_llm_model_calls_status",
        ),
        sa.CheckConstraint(
            "reserved_tokens > 0 AND charged_tokens >= 0", name="ck_llm_model_calls_budget"
        ),
        sa.CheckConstraint(
            "(prompt_tokens IS NULL AND completion_tokens IS NULL AND total_tokens IS NULL) OR "
            "(prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL AND "
            "total_tokens IS NOT NULL AND prompt_tokens >= 0 AND completion_tokens >= 0 AND "
            "total_tokens = prompt_tokens + completion_tokens)",
            name="ck_llm_model_calls_usage",
        ),
        sa.CheckConstraint(
            "(cache_hit_tokens IS NULL AND cache_miss_tokens IS NULL) OR "
            "(cache_hit_tokens IS NOT NULL AND cache_miss_tokens IS NOT NULL AND "
            "prompt_tokens IS NOT NULL AND cache_hit_tokens >= 0 AND cache_miss_tokens >= 0 AND "
            "cache_hit_tokens + cache_miss_tokens = prompt_tokens)",
            name="ck_llm_model_calls_cache",
        ),
        sa.ForeignKeyConstraint(
            ["admission_id"], ["llm_review_admissions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_model_calls_project_id", "llm_model_calls", ["project_id"])
    op.create_index("ix_llm_model_calls_day", "llm_model_calls", ["day"])
    op.drop_constraint("ck_review_jobs_status", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_status",
        "review_jobs",
        "status IN ('queued', 'running', 'retry_wait', 'paused', 'succeeded', 'failed')",
    )
    op.drop_constraint("ck_review_jobs_error", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_error",
        "review_jobs",
        "last_error IS NULL OR last_error IN ('pipeline_not_ready', 'execution_error', "
        "'timeout', 'lease_expired', 'stale_workflow', 'quota_exceeded')",
    )


def downgrade() -> None:
    # 旧版没有暂停状态；先停留为未完成，不让旧worker误判通过。
    op.execute(
        "UPDATE review_jobs SET status='failed', last_error='execution_error', "
        "finished_at=now() WHERE status='paused'"
    )
    op.drop_constraint("ck_review_jobs_error", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_error",
        "review_jobs",
        "last_error IS NULL OR last_error IN ('pipeline_not_ready', 'execution_error', "
        "'timeout', 'lease_expired', 'stale_workflow')",
    )
    op.drop_constraint("ck_review_jobs_status", "review_jobs", type_="check")
    op.create_check_constraint(
        "ck_review_jobs_status",
        "review_jobs",
        "status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
    )
    op.drop_table("llm_model_calls")
    op.drop_table("llm_review_admissions")
    op.drop_table("llm_daily_budgets")
    op.drop_table("llm_project_policies")
