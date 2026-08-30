"""Harden job lifecycle, output revisions, quality metadata and portable paths."""

import sqlalchemy as sa
from alembic import op

revision = "20260829_0002"
down_revision = "20260828_0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    _add("jobs", sa.Column("max_start_delay_ms", sa.Integer(), nullable=False, server_default="1000"))
    _add("jobs", sa.Column("control_requested", sa.Text(), nullable=True))
    _add("jobs", sa.Column("current_cue_id", sa.Integer(), nullable=True))
    _add("jobs", sa.Column("active_output_revision", sa.Text(), nullable=True))
    _add("jobs", sa.Column("pipeline_revision", sa.Text(), nullable=False, server_default="legacy-v0.4.4"))
    _add("jobs", sa.Column("glossary_json", sa.Text(), nullable=False, server_default="[]"))
    _add("jobs", sa.Column("glossary_revision", sa.Integer(), nullable=False, server_default="0"))
    _add("settings", sa.Column("max_start_delay_ms", sa.Integer(), nullable=False, server_default="1000"))
    _add("cues", sa.Column("effective_seed", sa.Integer(), nullable=True))
    _add("cues", sa.Column("generation_revision", sa.Integer(), nullable=False, server_default="0"))
    _add("cues", sa.Column("inference_text", sa.Text(), nullable=True))
    _add("cues", sa.Column("duration_multiplier", sa.Float(), nullable=True))
    _add("cues", sa.Column("generation_passes", sa.Integer(), nullable=False, server_default="0"))
    _add("cues", sa.Column("tail_metrics_json", sa.Text(), nullable=False, server_default="{}"))
    _add("cues", sa.Column("generation_duration_ms", sa.Integer(), nullable=True))
    _add("cues", sa.Column("cache_key", sa.Text(), nullable=True))
    _add("cues", sa.Column("requested_duration_multiplier", sa.Float(), nullable=True))
    _add("cues", sa.Column("pipeline_revision", sa.Text(), nullable=False, server_default="legacy-v0.4.4"))
    _add("audio_cache", sa.Column("quality_json", sa.Text(), nullable=False, server_default="{}"))
    _add("audio_cache", sa.Column("format_revision", sa.Text(), nullable=False, server_default="legacy"))
    _add("audio_cache", sa.Column("last_accessed_at", sa.Text(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_cues_job_status_position ON cues(job_id,status,position)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs(status,created_at)")
    for legacy_column in ("voice", "style"):
        if legacy_column in _columns("jobs"):
            op.drop_column("jobs", legacy_column)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jobs_status_created")
    op.execute("DROP INDEX IF EXISTS ix_cues_job_status_position")
