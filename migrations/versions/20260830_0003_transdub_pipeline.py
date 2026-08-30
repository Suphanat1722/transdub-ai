"""Add the end-to-end TransDub video pipeline."""

import sqlalchemy as sa
from alembic import op

revision = "20260830_0003"
down_revision = "20260829_0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    job_columns = [
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("original_audio_path", sa.Text(), nullable=True),
        sa.Column("background_path", sa.Text(), nullable=True),
        sa.Column("source_srt_path", sa.Text(), nullable=True),
        sa.Column("translated_srt_path", sa.Text(), nullable=True),
        sa.Column("dub_audio_path", sa.Text(), nullable=True),
        sa.Column("output_video_path", sa.Text(), nullable=True),
        sa.Column("video_duration_ms", sa.Integer(), nullable=True),
        sa.Column("video_codec", sa.Text(), nullable=True),
        sa.Column("source_language", sa.Text(), nullable=False, server_default="auto"),
        sa.Column("target_language", sa.Text(), nullable=False, server_default="th"),
        sa.Column("pause_after_transcription", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pause_after_translation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transcript_approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("translation_approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("background_volume", sa.Float(), nullable=False, server_default="100"),
        sa.Column("voice_volume", sa.Float(), nullable=False, server_default="100"),
        sa.Column("stage", sa.Text(), nullable=False, server_default="uploaded"),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("quota_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("translation_model", sa.Text(), nullable=True),
        sa.Column("video_stream_copied", sa.Integer(), nullable=True),
    ]
    for column in job_columns:
        _add("jobs", column)

    _add("cues", sa.Column("source_cue_indexes_json", sa.Text(), nullable=False, server_default="[]"))
    _add("cues", sa.Column("translation_chunk_id", sa.Text(), nullable=True))

    op.execute(
        """CREATE TABLE IF NOT EXISTS source_cues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            source_index TEXT NOT NULL,
            start_ms INTEGER NOT NULL,
            end_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            speaker TEXT,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(job_id, position)
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS translation_chunks (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            target_start INTEGER NOT NULL,
            target_end INTEGER NOT NULL,
            context_start INTEGER NOT NULL,
            context_end INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            model TEXT,
            output_cue_count INTEGER,
            error TEXT,
            UNIQUE(job_id, chunk_index)
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS stage_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            unit_id TEXT,
            model TEXT,
            outcome TEXT NOT NULL,
            message TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            thought_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
            stage TEXT NOT NULL,
            model TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            audio_seconds REAL NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        )"""
    )
    op.execute(
        """CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            media_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, kind)
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_source_cues_job_position ON source_cues(job_id,position)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_translation_chunks_job_status ON translation_chunks(job_id,status,chunk_index)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_stage_attempts_job_stage ON stage_attempts(job_id,stage,created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_api_usage_time ON api_usage(requested_at)")


def downgrade() -> None:
    for table in ("artifacts", "api_usage", "stage_attempts", "translation_chunks", "source_cues"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
