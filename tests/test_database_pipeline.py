from __future__ import annotations

from pathlib import Path

import app.core.config as config
from app.repositories import database as db


def configure_temp_data(monkeypatch, root: Path) -> None:
    mapping = {
        "DATA_DIR": root,
        "DB_PATH": root / "app.db",
        "JOBS_DIR": root / "jobs",
        "CACHE_DIR": root / "cache",
        "LOG_DIR": root / "logs",
        "OUTPUTS_DIR": root / "outputs",
    }
    for name, value in mapping.items():
        monkeypatch.setattr(config, name, value, raising=False)
        if hasattr(db, name):
            monkeypatch.setattr(db, name, value)


def test_job_cues_chunks_usage_and_artifacts(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db()
    video = tmp_path / "jobs" / "job" / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    job = db.create_video_job(
        job_id="job", filename="video.mp4", source_path=video,
        source_language="auto", pause_after_transcription=False, pause_after_translation=True,
        background_volume=100, voice_volume=90, voice="th-TH-PremwadeeNeural",
    )
    assert job["engine"] == "transdub"
    assert job["pause_after_translation"] is True
    assert job["voice"] == "th-TH-PremwadeeNeural"
    db.replace_source_cues("job", [
        {"source_index": 1, "start_ms": 0, "end_ms": 1000, "text": "Hello", "warnings": []}
    ])
    source = db.source_cues("job")
    assert source[0]["text"] == "Hello"
    db.replace_translation_cues("job", [
        {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1],
         "translation_chunk_id": "chunk-1", "warnings": []}
    ])
    cues, total = db.list_cues("job", limit=10, offset=0)
    assert total == 1 and cues[0]["source_cue_indexes"] == [1]
    artifact = tmp_path / "jobs" / "job" / "artifacts" / "source.srt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("srt", encoding="utf-8")
    db.put_artifact("job", "source_srt", artifact, "text/srt")
    assert db.get_artifact("job", "source_srt")["kind"] == "source_srt"

    db.replace_translation_chunks("job", [
        {
            "id": "chunk-a", "index": 0, "target_start": 0, "target_end": 0,
            "context_start": 0, "context_end": 0,
        },
        {
            "id": "chunk-b", "index": 1, "target_start": 1, "target_end": 1,
            "context_start": 0, "context_end": 1,
        },
    ])
    db.update_translation_chunk("chunk-b", status="completed", model="test-model")
    db.sync_translation_chunks("job", [
        {
            "id": "chunk-a-left", "index": 0, "target_start": 0, "target_end": 0,
            "context_start": 0, "context_end": 0,
        },
        {
            "id": "chunk-a-right", "index": 1, "target_start": 1, "target_end": 1,
            "context_start": 0, "context_end": 1,
        },
        {
            "id": "chunk-b", "index": 2, "target_start": 2, "target_end": 2,
            "context_start": 1, "context_end": 2,
        },
    ])
    chunks = db.translation_chunks("job")
    assert [chunk["id"] for chunk in chunks] == ["chunk-a-left", "chunk-a-right", "chunk-b"]
    assert chunks[-1]["status"] == "completed"
    assert chunks[-1]["chunk_index"] == 2

    db.update_job("job", status="transcribing", stage="separated", wait_reason=None)
    db.init_db()
    recovered = db.get_job("job", include_cues=False)
    assert recovered is not None
    assert recovered["status"] == "queued"
    assert recovered["stage"] == "separated"
    assert recovered["wait_reason"] == "กู้คืนงานหลังเปิดโปรแกรมใหม่"


def _make_job(db, tmp_path: Path, job_id: str, cache_key: str | None) -> None:
    video = tmp_path / "jobs" / job_id / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    db.create_video_job(
        job_id=job_id, filename="video.mp4", source_path=video,
        source_language="auto", pause_after_transcription=False, pause_after_translation=True,
        background_volume=100, voice_volume=90,
    )
    db.replace_translation_cues(job_id, [
        {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1],
         "translation_chunk_id": "chunk-1", "warnings": []}
    ])
    if cache_key is not None:
        cues, _total = db.list_cues(job_id, limit=1)
        db.update_cue(cues[0]["id"], cache_key=cache_key)


def test_delete_job_purges_unreferenced_audio_cache(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db()

    cache = config.CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)

    kept_wav = cache / "kept.wav"
    orphan_wav = cache / "orphan.wav"
    kept_wav.write_bytes(b"kept")
    orphan_wav.write_bytes(b"orphan")
    db.cache_put("kept-key", str(kept_wav), 1000)
    db.cache_put("orphan-key", str(orphan_wav), 800)

    # A cue in this job references kept-key.  After the job is deleted the cue
    # is gone too, so both cache entries become unreferenced and must be removed.
    _make_job(db, tmp_path, "job", "kept-key")

    assert db.delete_job("job") is True
    with db.connect() as conn:
        left = conn.execute(
            "SELECT COUNT(*) FROM audio_cache WHERE cache_key IN ('kept-key','orphan-key')"
        ).fetchone()[0]
    assert left == 0
    assert not kept_wav.exists()
    assert not orphan_wav.exists()


def test_purge_cache_keeps_entries_referenced_by_other_jobs(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db()

    cache = config.CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    shared_wav = cache / "shared.wav"
    shared_wav.write_bytes(b"shared")
    db.cache_put("shared-key", str(shared_wav), 1000)

    _make_job(db, tmp_path, "jobA", None)
    _make_job(db, tmp_path, "jobB", "shared-key")

    # Deleting jobA must NOT remove the cache entry still referenced by jobB.
    assert db.delete_job("jobA") is True
    assert shared_wav.exists()
    with db.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM audio_cache WHERE cache_key='shared-key'").fetchone()[0]
    assert left == 1


def test_backup_database_prunes_to_five(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db()  # itself backs up once
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for day in range(7):
        (backup_dir / f"app-2020010{day}.db").write_bytes(b"old")
    result = db.backup_database()
    assert result is not None and result.is_file()
    remaining = sorted(path.name for path in backup_dir.glob("app-*.db"))
    assert len(remaining) == 5
    assert result.name in remaining
