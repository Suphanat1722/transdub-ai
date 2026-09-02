"""Add job mode column to support imported-SRT vs Gemini translation."""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0007"
down_revision = "20260902_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "mode" not in columns:
        op.add_column(
            "jobs",
            sa.Column("mode", sa.Text(), nullable=False, server_default="normal"),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "mode" in columns:
        op.drop_column("jobs", "mode")