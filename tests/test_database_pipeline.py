from __future__ import annotations

from pathlib import Path

import app.core.config as config
from app.repositories import database as db


def configure_temp_data(monkeypatch, root: Path) -> None:
    mapping = {
        "DATA_DIR": root,
        "DB_PATH": root / "app.db",
        "JOBS_DIR": root / "jobs",
        "PROFILES_DIR": root / "voice-profiles",
        "CACHE_DIR": root / "cache",
        "IMPORTS_DIR": root / "imports",
        "LOG_DIR": root / "logs",
        "MEDIA_CACHE_DIR": root / "media-cache",
    }
    for name, value in mapping.items():
        monkeypatch.setattr(config, name, value, raising=False)
        if hasattr(db, name):
            monkeypatch.setattr(db, name, value)


def test_job_cues_chunks_usage_and_artifacts(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db(run_legacy_migration=False)
    profile_audio = tmp_path / "voice-profiles" / "voice.wav"
    profile_audio.parent.mkdir(parents=True, exist_ok=True)
    profile_audio.write_bytes(b"wav")
    db.create_voice_profile("voice", "Voice", "อ้างอิง", config.data_relative(profile_audio), "hash", 1000, [])
    video = tmp_path / "jobs" / "job" / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    job = db.create_video_job(
        job_id="job", filename="video.mp4", source_path=video, voice_profile_id="voice",
        source_language="auto", pause_after_transcription=False, pause_after_translation=True,
        background_volume=100, voice_volume=90,
    )
    assert job["engine"] == "transdub"
    assert job["pause_after_translation"] is True
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

    db.update_job("job", status="transcribing", stage="separated", wait_reason=None)
    db.init_db(run_legacy_migration=False)
    recovered = db.get_job("job", include_cues=False)
    assert recovered is not None
    assert recovered["status"] == "queued"
    assert recovered["stage"] == "separated"
    assert recovered["wait_reason"] == "กู้คืนงานหลังเปิดโปรแกรมใหม่"
