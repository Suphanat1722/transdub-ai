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

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _setup(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker_module, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db()


def _mock_youtube(monkeypatch, tmp_path: Path, srt_text: str, language: str) -> None:
    from types import SimpleNamespace

    def fake_download(url: str, target_dir: Path, progress=None) -> tuple[Path, str | None]:
        video = target_dir / "youtube.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        if progress:
            progress(100.0)
        return video, "Some Clip Title"

    monkeypatch.setattr(worker_module, "download_video", fake_download)
    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda path: SimpleNamespace(
            has_video=True, has_audio=True, duration=10.0, video_codec="h264"
        ),
    )
    monkeypatch.setattr(
        worker_module, "fetch_subtitle", lambda url, source_language="auto": (srt_text, language)
    )


def test_create_job_stores_translation_prompt(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/jobs",
            data={"youtube_url": URL, "translation_prompt": "ใช้ภาษาไทยสุภาพ"},
        )
        assert resp.status_code == 202
        assert resp.json()["translation_prompt"] == "ใช้ภาษาไทยสุภาพ"


def test_non_thai_youtube_worker_pauses_at_transcript_review(monkeypatch, tmp_path: Path) -> None:
    """A non-Thai subtitle lands on transcript review so the user can set the
    Gemini prompt before translating."""
    _setup(monkeypatch, tmp_path)
    srt = "1\n00:00:00,000 --> 00:00:01,000\nHello there\n"
    _mock_youtube(monkeypatch, tmp_path, srt, "en")
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs", data={"youtube_url": URL, "pause_after_transcription": "true"})
        assert resp.status_code == 202
        job_id = resp.json()["id"]

    # Drive the worker through download and separation, then stop at review.
    worker._process_one(job_id)
    job = db.get_job(job_id)
    assert job is not None
    assert job["stage"] == "downloaded"
    assert job["mode"] == "import_pending"
    db.update_job(job_id, stage="separated", status="queued")
    worker._process_one(job_id)
    job = db.get_job(job_id)
    assert job is not None
    assert job["stage"] == "transcribed"
    assert job["status"] == "reviewing_transcript"
    assert job["transcript_approved"] == 0
    assert job["source_srt_path"] is not None


def test_retranslate_resets_translation_and_queues(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    _make_translated_job(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs/j1/actions", json={"action": "retranslate"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "queued"
        assert data["stage"] == "transcribed"
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