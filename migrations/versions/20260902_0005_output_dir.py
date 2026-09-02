"""Add optional output directory for finished videos."""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0005"
down_revision = "20260902_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "output_dir" not in columns:
        op.add_column("jobs", sa.Column("output_dir", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "output_dir" in columns:
        op.drop_column("jobs", "output_dir")