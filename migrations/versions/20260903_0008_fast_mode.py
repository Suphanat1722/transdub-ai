"""Add separation_mode for fast jobs that skip Demucs."""

import sqlalchemy as sa
from alembic import op

revision = "20260903_0008"
down_revision = "20260902_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "separation_mode" not in columns:
        op.add_column(
            "jobs",
            sa.Column("separation_mode", sa.Text(), nullable=False, server_default="demucs"),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "separation_mode" in columns:
        op.drop_column("jobs", "separation_mode")
