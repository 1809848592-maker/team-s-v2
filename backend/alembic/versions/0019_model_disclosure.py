"""M3-13 explicit project consent and first-submission disclosure.

Revision ID: 0019_model_disclosure
Revises: 0018_version_binding
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0019_model_disclosure"
down_revision = "0018_version_binding"
branch_labels = None
depends_on = None


def upgrade():
    for table, person, timestamp in (
        ("project_model_consents", "actor_id", "accepted_at"),
        ("submission_disclosures", "user_id", "acknowledged_at"),
    ):
        columns = [
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                person, sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
            ),
            sa.Column("disclosure_version", sa.String(80), nullable=False),
            sa.Column("notice", JSONB(), nullable=False),
            sa.Column(
                "event_id",
                sa.BigInteger(),
                sa.ForeignKey("events.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                timestamp, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
            ),
        ]
        if table == "submission_disclosures":
            columns.append(
                sa.Column(
                    "delivery_id",
                    sa.Uuid(),
                    sa.ForeignKey("delivery_versions.id", ondelete="RESTRICT"),
                    nullable=False,
                )
            )
            constraint = sa.UniqueConstraint(
                "project_id", "user_id", "disclosure_version", name="uq_submission_disclosure_user"
            )
        else:
            constraint = sa.UniqueConstraint(
                "project_id", "disclosure_version", name="uq_project_consent_version"
            )
        op.create_table(table, *columns, constraint)
        op.create_index(f"ix_{table}_project_id", table, ["project_id"])
        op.execute(
            f"CREATE FUNCTION reject_{table}_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'published snapshots are immutable' "
            "USING ERRCODE = '55000'; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION reject_{table}_mutation()"
        )


def downgrade():
    for table in ("submission_disclosures", "project_model_consents"):
        op.drop_table(table)
        op.execute(f"DROP FUNCTION reject_{table}_mutation()")
