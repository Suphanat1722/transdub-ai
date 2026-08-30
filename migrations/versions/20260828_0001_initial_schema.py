"""Create the JaiCue Studio schema."""

from alembic import op

revision = "20260828_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = [
        "CREATE TABLE migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL, details_json TEXT NOT NULL)",
        """CREATE TABLE voice_profiles (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, transcript TEXT NOT NULL,
            audio_path TEXT NOT NULL, audio_hash TEXT NOT NULL, duration_ms INTEGER NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
        )""",
        """CREATE TABLE jobs (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, encoding TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', warnings_json TEXT NOT NULL DEFAULT '[]', error TEXT,
            wait_reason TEXT, next_attempt_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, completed_at TEXT, voice_profile_id TEXT REFERENCES voice_profiles(id),
            nfe_step INTEGER NOT NULL DEFAULT 32, inference_speed REAL NOT NULL DEFAULT 1.0,
            seed INTEGER NOT NULL DEFAULT 0, engine TEXT NOT NULL DEFAULT 'jaitts'
        )""",
        """CREATE TABLE cues (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, source_index TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
            text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', warnings_json TEXT NOT NULL DEFAULT '[]',
            audio_path TEXT, original_duration_ms INTEGER, final_duration_ms INTEGER,
            speed_factor REAL NOT NULL DEFAULT 1.0, attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, seed INTEGER NOT NULL DEFAULT 0, UNIQUE(job_id, position)
        )""",
        """CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id=1), nfe_step INTEGER NOT NULL DEFAULT 32,
            inference_speed REAL NOT NULL DEFAULT 1.0, allow_cpu INTEGER NOT NULL DEFAULT 1
        )""",
        "INSERT INTO settings(id) VALUES(1)",
        """CREATE TABLE audio_cache (
            cache_key TEXT PRIMARY KEY, path TEXT NOT NULL, duration_ms INTEGER NOT NULL, created_at TEXT NOT NULL
        )""",
    ]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    for table in ("audio_cache", "settings", "cues", "jobs", "voice_profiles", "migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
