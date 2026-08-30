"""Import reusable JaiCue voice profiles without importing historical jobs."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from app.core.config import PROFILES_DIR, data_relative
from app.repositories import database


def main() -> None:
    legacy_root = Path(os.getenv("LEGACY_TTS_ROOT", r"E:\Gemini TTS")).resolve()
    legacy_db = legacy_root / "data" / "app.db"
    if not legacy_db.is_file():
        print(f"ไม่พบฐานข้อมูลเดิม: {legacy_db}")
        return
    database.init_db(run_legacy_migration=False)
    source = sqlite3.connect(legacy_db)
    source.row_factory = sqlite3.Row
    rows = source.execute("SELECT * FROM voice_profiles ORDER BY created_at").fetchall()
    imported = 0
    for row in rows:
        if database.get_voice_profile(row["id"]):
            continue
        old_audio = Path(row["audio_path"])
        if not old_audio.is_absolute():
            old_audio = legacy_root / "data" / old_audio
        target_dir = PROFILES_DIR / row["id"]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "reference.wav"
        if not target.is_file():
            shutil.copy2(old_audio, target)
        database.create_voice_profile(
            row["id"],
            row["name"],
            row["transcript"],
            data_relative(target),
            row["audio_hash"],
            int(row["duration_ms"]),
            json.loads(row["warnings_json"] or "[]"),
        )
        imported += 1
    print(f"นำเข้าโปรไฟล์เสียง {imported} รายการ (พบทั้งหมด {len(rows)})")


if __name__ == "__main__":
    main()
