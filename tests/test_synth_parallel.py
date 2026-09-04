"""Parallel Edge TTS synthesis: a worker pass completes a batch on many threads."""

from __future__ import annotations

import threading
import wave
from pathlib import Path

import app.services.worker as worker_module
from app.repositories import database as db
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data


def _setup(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(worker_module, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker_module, "CACHE_DIR", tmp_path / "cache")
    db.init_db()


def test_synthesize_batch_runs_concurrently(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    job = db.create_video_job(
        job_id="parallel", filename="v.mp4", source_path="https://x", source_language="en",
        pause_after_transcription=False, pause_after_translation=False,
        background_volume=100, voice_volume=100,
    )
    db.replace_translation_cues(job["id"], [
        {"start_ms": i * 1000, "end_ms": i * 1000 + 800, "text": f"ประโยคที่ {i}",
         "source_cue_indexes": [i + 1], "translation_chunk_id": "c0", "warnings": []}
        for i in range(4)
    ])

    idents: set[int] = set()
    lock = threading.Lock()

    def fake_synth(text: str, voice: str, rate: int, output: Path, timeout: float = 60) -> int:
        with lock:
            idents.add(threading.get_ident())
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(b"\x00\x00" * 2400)
        return 100

    monkeypatch.setattr(worker_module, "synth_cue", fake_synth)
    worker._synthesize(db.get_job(job["id"]))

    counts = db.cue_counts(job["id"])
    assert counts.get("completed", 0) == 4
    assert len(idents) > 1
