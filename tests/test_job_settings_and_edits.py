from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
from app.main import create_app
from app.repositories import database as db
from app.services.translation import serialize_srt
from app.services.worker import worker

from .test_audio import tone, write_pcm_wav
from .test_database_pipeline import configure_temp_data


def make_client(monkeypatch, tmp_path: Path) -> TestClient:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db()
    return TestClient(create_app())


def seed_job(job_id: str, tmp_path: Path, *, pause_transcript: bool = True) -> None:
    """Create a job with source + translated cues at the transcript-review stage."""
    video = tmp_path / "jobs" / job_id / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    db.create_video_job(
        job_id=job_id, filename="video.mp4", source_path=video,
        source_language="auto", pause_after_transcription=pause_transcript,
        pause_after_translation=True, background_volume=100, voice_volume=100,
    )
    db.replace_source_cues(job_id, [
        {"source_index": 1, "start_ms": 0, "end_ms": 1000, "text": "Hello", "warnings": []},
        {"source_index": 2, "start_ms": 1200, "end_ms": 2000, "text": "World", "warnings": []},
    ])
    db.replace_translation_cues(job_id, [
        {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1], "warnings": []},
        {"start_ms": 1200, "end_ms": 2000, "text": "สวัสดีชาวโลก", "source_cue_indexes": [2], "warnings": []},
    ])
    artifacts = tmp_path / "jobs" / job_id / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "translated.th.srt").write_text("srt", encoding="utf-8")
    db.put_artifact(job_id, "translated_srt", artifacts / "translated.th.srt", "application/x-subrip")
    (artifacts / "dub.wav").write_bytes(b"wav")
    db.put_artifact(job_id, "dub_wav", artifacts / "dub.wav", "audio/wav")
    source_srt = artifacts / "source.srt"
    source_srt.write_text(serialize_srt(db.source_cues(job_id), bom=True), encoding="utf-8")
    db.update_job(job_id, source_srt_path="jobs/" + job_id + "/artifacts/source.srt")
    db.update_job(job_id, stage="transcribed", status="reviewing_transcript", transcript_approved=0)


def test_edit_source_cue_invalidates_translation(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path)
    source_cue = db.source_cues("job")[0]

    response = client.patch(
        f"/api/jobs/job/cues/{source_cue['id']}",
        json={"layer": "source", "text": "Hello there", "start_ms": 0, "end_ms": 900},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stage"] == "transcribed"
    assert body["status"] == "reviewing_transcript"

    updated = db.source_cues("job")[0]
    assert updated["text"] == "Hello there"
    assert updated["end_ms"] == 900
    # The translation layer and its downstream outputs must be invalidated.
    _, total = db.list_cues("job", limit=10)
    assert total == 0
    assert db.get_artifact("job", "translated_srt") is None
    assert db.get_artifact("job", "dub_wav") is None


def test_retranslate_clears_translation_and_requeues(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path)
    # Move the job to the translation-review stage as if translation had run.
    db.update_job("job", stage="translated", status="reviewing_translation", translation_approved=0)

    response = client.post("/api/jobs/job/actions", json={"action": "retranslate"})
    assert response.status_code == 202
    body = response.json()
    assert body["stage"] == "translated"
    assert body["status"] == "queued"
    assert body["translation_approved"] is False
    _, total = db.list_cues("job", limit=10)
    assert total == 0
    assert db.translation_chunks("job") == []
    assert db.get_artifact("job", "translated_srt") is None
    assert db.get_artifact("job", "dub_wav") is None


def test_patch_voice_invalidates_generated_audio(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path)
    # Simulate cues whose speech was already generated.
    for cue in db.get_job("job")["cues"]:
        audio = tmp_path / "jobs" / "job" / "cues" / f"{cue['position']:05d}.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        write_pcm_wav(audio, tone(200))
        db.update_cue(
            cue["id"], status="completed", audio_path=f"jobs/job/cues/{cue['position']:05d}.wav",
            final_duration_ms=200, generation_revision=3,
        )
    dub = tmp_path / "jobs" / "job" / "artifacts" / "dub.wav"
    write_pcm_wav(dub, tone(100))
    db.put_artifact("job", "dub_wav", dub, "audio/wav")
    db.update_job("job", stage="synthesized", status="needs_review", translation_approved=1)

    response = client.patch("/api/jobs/job", json={"voice": "th-TH-PremwadeeNeural", "tts_rate": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["voice"] == "th-TH-PremwadeeNeural"
    assert body["tts_rate"] == 10
    assert body["stage"] == "translated"
    assert body["status"] == "queued"

    for cue in db.get_job("job")["cues"]:
        assert cue["status"] == "pending"
        assert cue["audio_path"] is None
        assert cue["generation_revision"] == 4
    assert db.get_artifact("job", "dub_wav") is None


def test_patch_volumes_keeps_generated_audio(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path)
    cue = db.get_job("job")["cues"][0]
    db.update_cue(cue["id"], status="completed", final_duration_ms=200)
    db.update_job("job", stage="translated", status="paused", translation_approved=0)

    response = client.patch("/api/jobs/job", json={"background_volume": 40, "voice_volume": 120})
    assert response.status_code == 200
    body = response.json()
    assert body["background_volume"] == 40
    assert body["voice_volume"] == 120
    assert body["status"] == "paused"  # untouched: only volumes changed
    assert db.get_cue(cue["id"])["status"] == "completed"


def test_patch_rejects_active_job_and_bad_folder(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path)
    db.update_job("job", status="translating")
    assert client.patch("/api/jobs/job", json={"voice": "th-TH-PremwadeeNeural"}).status_code == 409
    db.update_job("job", status="reviewing_transcript")
    bad = client.patch("/api/jobs/job", json={"output_dir": "D:/definitely-missing-folder"})
    assert bad.status_code == 422


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_cue_preview_mixes_background(monkeypatch, tmp_path: Path) -> None:
    client = make_client(monkeypatch, tmp_path)
    seed_job("job", tmp_path, pause_transcript=False)
    db.update_job("job", stage="translated", status="queued", translation_approved=1)
    cue = db.get_job("job")["cues"][0]
    voice = tmp_path / "jobs" / "job" / "cues" / "00001.wav"
    voice.parent.mkdir(parents=True, exist_ok=True)
    write_pcm_wav(voice, tone(500))
    db.update_cue(
        cue["id"], status="completed", audio_path="jobs/job/cues/00001.wav",
        start_ms=0, end_ms=500, final_duration_ms=500,
    )
    background = tmp_path / "jobs" / "job" / "artifacts" / "background.flac"
    write_pcm_wav(tmp_path / "bg.wav", tone(4000))
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(tmp_path / "bg.wav"),
         "-c:a", "flac", str(background)],
        check=True, capture_output=True,
    )
    db.put_artifact("job", "background", background, "audio/flac")

    ready = client.get(f"/api/jobs/job/cues/{cue['id']}/preview")
    assert ready.status_code == 200
    assert ready.headers["content-type"] == "audio/wav"
    missing = client.get(f"/api/jobs/job/cues/{cue['id'] + 999}/preview")
    assert missing.status_code == 404
