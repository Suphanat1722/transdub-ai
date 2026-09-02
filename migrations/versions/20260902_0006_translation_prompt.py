"""Add per-job translation prompt column."""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0006"
down_revision = "20260902_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "translation_prompt" not in columns:
        op.add_column(
            "jobs",
            sa.Column("translation_prompt", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "translation_prompt" in columns:
        op.drop_column("jobs", "translation_prompt")