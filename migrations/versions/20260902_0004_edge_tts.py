"""Switch speech synthesis to Edge TTS preset voices and drop voice cloning.

Edge TTS offers no voice cloning, so the reference-audio cloning stack (voice
profiles, per-cue effective seeds, duration passes/multipliers, tail heuristics,
model/checkpoint fields and the audio cache that held generated WAVs) is no
longer needed.  Jobs now carry a preset Edge voice and a signed TTS rate instead.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260902_0004"
down_revision = "20260830_0003"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def _drop(table: str, name: str) -> None:
    if name in _columns(table):
        op.drop_column(table, name)


def upgrade() -> None:
    # Keep the foreign key on jobs.voice_profile_id: SQLite cannot drop a column
    # that is part of a foreign key after the table exists, so the profile id is
    # left behind as an unused reference instead of being removed.

    _add("jobs", sa.Column("voice", sa.Text(), nullable=False, server_default="th-TH-NiwatNeural"))
    _add("jobs", sa.Column("tts_rate", sa.Integer(), nullable=False, server_default="0"))

    op.execute(
        """CREATE TABLE IF NOT EXISTS voice_settings (
            id INTEGER PRIMARY KEY CHECK (id=1),
            voice TEXT NOT NULL DEFAULT 'th-TH-NiwatNeural',
            tts_rate INTEGER NOT NULL DEFAULT 0
        )"""
    )
    op.execute("INSERT OR IGNORE INTO voice_settings(id) VALUES(1)")

    cue_columns = [
        "effective_seed",
        "duration_multiplier",
        "generation_passes",
        "tail_metrics_json",
        "requested_duration_multiplier",
    ]
    for column in cue_columns:
        _drop("cues", column)


def downgrade() -> None:
    for column in (
        "requested_duration_multiplier",
        "tail_metrics_json",
        "generation_passes",
        "duration_multiplier",
        "effective_seed",
    ):
        _add("cues", sa.Column(column, sa.Text(), nullable=True))
    op.execute("DROP TABLE IF EXISTS voice_settings")
    _drop("jobs", "tts_rate")
    _drop("jobs", "voice")