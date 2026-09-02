from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
import app.services.translation as translation
from app.main import create_app
from app.repositories import database as db
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data


def _setup_job(monkeypatch, tmp_path: Path, job_id: str = "j1") -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db(run_legacy_migration=False)
    video = tmp_path / "jobs" / job_id / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    db.create_video_job(
        job_id=job_id, filename="v.mp4", source_path=video,
        source_language="auto", pause_after_transcription=False,
        pause_after_translation=False, background_volume=100, voice_volume=100,
        voice="th-TH-NiwatNeural",
    )
    db.replace_source_cues(job_id, [
        {"source_index": 1, "start_ms": 0, "end_ms": 1000, "text": "Hello", "warnings": []}
    ])


def test_import_translation_srt_replaces_cues_and_parks_review(monkeypatch, tmp_path: Path) -> None:
    _setup_job(monkeypatch, tmp_path)
    srt = "1\n00:00:00,000 --> 00:00:01,000\n\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\n".encode()
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs/j1/translation-srt",
            files={"file": ("th.srt", srt, "text/plain")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reviewing_translation"
        assert data["stage"] == "translated"
        job = db.get_job("j1")
        assert job is not None
        assert job["cues"][0]["text"] == "สวัสดี"


def test_import_translation_srt_rejects_invalid(monkeypatch, tmp_path: Path) -> None:
    _setup_job(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs/j1/translation-srt",
            files={"file": ("bad.srt", b"not a valid srt", "text/plain")},
        )
        assert resp.status_code == 422


def test_retranslate_resets_translation_and_queues(monkeypatch, tmp_path: Path) -> None:
    _setup_job(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        # Place the job in the translated review state first.
        db.replace_translation_cues("j1", [
            {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1],
             "translation_chunk_id": None, "warnings": []}
        ])
        db.update_job("j1", stage="translated", status="reviewing_translation")
        resp = client.post("/api/jobs/j1/actions", json={"action": "retranslate"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["stage"] == "translated"
        assert data["translation_approved"] is False
        # cues cleared so worker re-translates from source
        job = db.get_job("j1")
        assert job is not None and job["cues"] == []


def test_update_and_return_translation_prompt(monkeypatch, tmp_path: Path) -> None:
    _setup_job(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.put(
            "/api/jobs/j1/translation-prompt",
            json={"prompt": "ใช้ภาษาไทยสุภาพ กระชับ"},
        )
        assert resp.status_code == 200
        assert resp.json()["translation_prompt"] == "ใช้ภาษาไทยสุภาพ กระชับ"
        job = db.get_job("j1", include_cues=False)
        assert job is not None and job["translation_prompt"] == "ใช้ภาษาไทยสุภาพ กระชับ"


def test_custom_prompt_is_used_by_request_else_default() -> None:
    source = [
        {"position": 1, "source_index": "1", "start_ms": 0, "end_ms": 1000, "text": "Hello."},
    ]
    chunk = translation.Chunk("c1", 0, 0, 0, 0, 0)
    system, _contents, _tokens = translation._request(source, chunk, "auto", [], "custom prompt")
    assert system == "custom prompt"
    system2, _contents2, _tokens2 = translation._request(source, chunk, "auto", [])
    assert system2 == translation.DEFAULT_TRANSLATION_PROMPT