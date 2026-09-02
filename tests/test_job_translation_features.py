from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
import app.services.translation as translation
import app.services.worker as worker_module
from app.main import create_app
from app.repositories import database as db
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data


def _setup(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker_module, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db(run_legacy_migration=False)


def test_create_job_with_srt_sets_import_mode_and_cues(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    srt = "1\n00:00:00,000 --> 00:00:01,000\n\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\n".encode()
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural", "pause_after_translation": "true"},
            files={
                "video": ("clip.mp4", b"not-probed", "video/mp4"),
                "srt": ("th.srt", srt, "text/plain"),
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["mode"] == "import"
        assert data["status"] == "queued"
        job_id = data["id"]
        job = db.get_job(job_id)
        assert job is not None
        # Cues preloaded as the translation layer from the imported SRT.
        assert job["cues"][0]["text"] == "สวัสดี"


def test_create_job_with_pending_srt_sets_import_pending(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    srt = b"1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural", "srt_mode": "pending"},
            files={
                "video": ("clip.mp4", b"not-probed", "video/mp4"),
                "srt": ("src.srt", srt, "text/plain"),
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["mode"] == "import_pending"
        assert data["status"] == "queued"
        # Untranslated-SRT mode must always stop to set the Gemini prompt and
        # review the source, and then to review the translation, even when the
        # UI sends no pause flags (e.g. cached older JS).  This guarantees the
        # job pauses for prompt entry before translating.
        assert data["pause_after_transcription"] is True
        assert data["pause_after_translation"] is True
        job_id = data["id"]
        job = db.get_job(job_id)
        assert job is not None
        # Source cues come from the imported (untranslated) SRT; translation empty.
        assert job["source_cues"][0]["text"] == "Hello there"
        assert job["cues"] == []


def test_pending_srt_worker_pauses_at_prompt_review(monkeypatch, tmp_path: Path) -> None:
    # For an untranslated SRT, the worker must stop at "reviewing_transcript"
    # (so the user can set the Gemini system prompt) before translating,
    # rather than jumping straight into Gemini.  The backend forces both
    # review pauses on for import_pending jobs.
    _setup(monkeypatch, tmp_path)
    srt = b"1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural", "srt_mode": "pending"},
            files={
                "video": ("clip.mp4", b"not-probed", "video/mp4"),
                "srt": ("src.srt", srt, "text/plain"),
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["id"]

    # Drive the worker just past ASR for import_pending: it should land on the
    # transcript-review step, not on the translation synthesis.
    db.update_job(job_id, stage="separated", status="queued")
    worker._process_one(job_id)
    job = db.get_job(job_id)
    assert job is not None
    assert job["stage"] == "transcribed"
    assert job["status"] == "reviewing_transcript"
    assert job["transcript_approved"] == 0
    assert job["source_srt_path"] is not None


def test_create_job_without_srt_uses_normal_mode(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural", "pause_after_translation": "true"},
            files={"video": ("clip.mp4", b"not-probed", "video/mp4")},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["mode"] == "normal"
        job_id = data["id"]
        job = db.get_job(job_id)
        assert job is not None and job["cues"] == []


def test_create_job_with_invalid_srt_rejected(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural"},
            files={
                "video": ("clip.mp4", b"not-probed", "video/mp4"),
                "srt": ("bad.srt", b"not a valid srt", "text/plain"),
            },
        )
        assert resp.status_code == 422


def test_create_job_stores_translation_prompt(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"voice": "th-TH-NiwatNeural", "translation_prompt": "ใช้ภาษาไทยสุภาพ"},
            files={"video": ("clip.mp4", b"not-probed", "video/mp4")},
        )
        assert resp.status_code == 202
        assert resp.json()["translation_prompt"] == "ใช้ภาษาไทยสุภาพ"


def test_retranslate_resets_translation_and_queues(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    _make_translated_job(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs/j1/actions", json={"action": "retranslate"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["stage"] == "translated"
        assert data["translation_approved"] is False
        job = db.get_job("j1")
        assert job is not None and job["cues"] == []


def test_update_and_return_translation_prompt(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    _make_basic_job(monkeypatch, tmp_path)
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


def _make_basic_job(monkeypatch, tmp_path: Path) -> None:
    video = tmp_path / "jobs" / "j1" / "source" / "video.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    db.create_video_job(
        job_id="j1", filename="v.mp4", source_path=video,
        source_language="auto", pause_after_transcription=False,
        pause_after_translation=False, background_volume=100, voice_volume=100,
        voice="th-TH-NiwatNeural",
    )
    db.replace_source_cues("j1", [
        {"source_index": 1, "start_ms": 0, "end_ms": 1000, "text": "Hello", "warnings": []}
    ])


def _make_translated_job(monkeypatch, tmp_path: Path) -> None:
    _make_basic_job(monkeypatch, tmp_path)
    db.replace_translation_cues("j1", [
        {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1],
         "translation_chunk_id": None, "warnings": []}
    ])
    db.update_job("j1", stage="translated", status="reviewing_translation")