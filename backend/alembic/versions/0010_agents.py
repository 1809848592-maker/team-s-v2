"""M2-01 智能体登记及可吊销的哈希令牌。

Revision ID: 0010_agents
Revises: 0009_collaboration
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0010_agents"
down_revision: str | None = "0009_collaboration"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("client_type", sa.String(80), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_agents_token_hash"),
    )
    op.create_index("ix_agents_project_id", "agents", ["project_id"])
    op.create_index("ix_agents_owner_id", "agents", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_agents_owner_id", table_name="agents")
    op.drop_index("ix_agents_project_id", table_name="agents")
    op.drop_table("agents")
