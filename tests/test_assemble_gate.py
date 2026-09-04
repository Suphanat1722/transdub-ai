"""Assemble overrun policy: small subtitle slop auto-extends, big drifts park."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

import app.services.worker as worker_module
from app.core.config import SAMPLE_RATE
from app.repositories import database as db
from app.services.audio import write_pcm_wav
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")


def _tone(duration_ms: int) -> bytes:
    import math

    frames = int(SAMPLE_RATE * duration_ms / 1000)
    return b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
        for i in range(frames)
    )


def _ready_job(monkeypatch, tmp_path: Path, job_id: str, audio_ms: int) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(worker_module, "JOBS_DIR", tmp_path / "jobs")
    db.init_db()
    db.create_video_job(
        job_id=job_id, filename="v.mp4", source_path="https://x", source_language="en",
        pause_after_transcription=False, pause_after_translation=False,
        background_volume=100, voice_volume=100,
    )
    db.replace_translation_cues(job_id, [
        {"start_ms": 1500, "end_ms": 2500, "text": "สวัสดี",
         "source_cue_indexes": [1], "translation_chunk_id": "c0", "warnings": []},
    ])
    cue = db.get_job(job_id)["cues"][0]
    wav = tmp_path / "jobs" / job_id / "cues" / "00001.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    write_pcm_wav(wav, _tone(audio_ms))
    db.update_cue(
        cue["id"], status="completed", audio_path=f"jobs/{job_id}/cues/00001.wav",
        original_duration_ms=audio_ms, final_duration_ms=audio_ms,
    )
    db.update_job(job_id, video_duration_ms=2000, video_codec="h264")


def test_small_overrun_proceeds_with_frozen_tail_note(monkeypatch, tmp_path: Path) -> None:
    _ready_job(monkeypatch, tmp_path, "small", audio_ms=1200)
    worker._assemble_dub(db.get_job("small"))
    job = db.get_job("small", include_cues=False)
    assert job["stage"] == "synthesized"
    assert job["status"] == "queued"
    assert any("ภาพนิ่ง" in warning for warning in job["warnings"])


def test_large_overrun_parks_for_review(monkeypatch, tmp_path: Path) -> None:
    _ready_job(monkeypatch, tmp_path, "big", audio_ms=4000)
    worker._assemble_dub(db.get_job("big"))
    job = db.get_job("big", include_cues=False)
    assert job["stage"] == "synthesizing"
    assert job["status"] == "needs_review"
