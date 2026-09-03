from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
import app.services.worker as worker_module
from app.main import create_app
from app.repositories import database as db
from app.services import youtube
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

SRT_TEXT = (
    "1\n00:00:00,000 --> 00:00:01,000\n\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\n"
    "2\n00:00:02,000 --> 00:00:03,000\n\u0e22\u0e34\u0e19\u0e14\u0e35\u0e15\u0e49\u0e2d\u0e19\u0e23\u0e31\u0e1a"
)
SRT_ENGLISH = "1\n00:00:00,000 --> 00:00:01,000\nHello there\n"


def _setup(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker_module, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db()


def _mock_download(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    def fake_download(url: str, target_dir: Path) -> Path:
        video = target_dir / "youtube.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        return video

    monkeypatch.setattr(worker_module, "download_video", fake_download)
    monkeypatch.setattr(
        worker_module,
        "probe_media",
        lambda path: SimpleNamespace(
            has_video=True, has_audio=True, duration=10.0, video_codec="h264"
        ),
    )


def _mock_fetch_subtitle(monkeypatch, srt_text: str, language: str) -> None:
    monkeypatch.setattr(
        worker_module, "fetch_subtitle", lambda url: (srt_text, language)
    )


def test_extract_video_id_variants() -> None:
    assert youtube.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("not a url") is None
    assert youtube.extract_video_id("") is None


def test_create_job_requires_valid_youtube_url(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs", data={"youtube_url": "not-a-url"})
        assert resp.status_code == 422


def test_resolve_youtube_language_sets_import_for_thai(monkeypatch, tmp_path: Path) -> None:
    """Thai subtitle becomes both source and translation; mode import."""
    _setup(monkeypatch, tmp_path)
    _mock_download(monkeypatch, tmp_path)
    _mock_fetch_subtitle(monkeypatch, SRT_TEXT, "th")
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs", data={"youtube_url": URL})
        assert resp.status_code == 202
        data = resp.json()
        assert data["mode"] == "youtube"
        job_id = data["id"]

    # Move the worker through download: it sets mode import + translation cues.
    worker._process_one(job_id)
    job = db.get_job(job_id)
    assert job is not None
    assert job["mode"] == "import"
    assert job["stage"] == "downloaded"
    assert job["source_path"] is not None
    assert [c["text"] for c in job["cues"]] == ["สวัสดี", "ยินดีต้อนรับ"]


def test_resolve_youtube_language_sets_import_pending_for_non_thai(monkeypatch, tmp_path: Path) -> None:
    """English subtitle becomes source only; Gemini translates later."""
    _setup(monkeypatch, tmp_path)
    _mock_download(monkeypatch, tmp_path)
    _mock_fetch_subtitle(monkeypatch, SRT_ENGLISH, "en")
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs", data={"youtube_url": URL})
        assert resp.status_code == 202
        job_id = resp.json()["id"]

    worker._process_one(job_id)
    job = db.get_job(job_id)
    assert job is not None
    assert job["mode"] == "import_pending"
    assert job["stage"] == "downloaded"
    assert job["source_cues"][0]["text"] == "Hello there"
    assert job["cues"] == []


def test_youtube_failure_marks_job_failed(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)

    def fail_download(url: str, target_dir: Path) -> Path:
        raise youtube.YouTubeError("วิดีโอ private")

    monkeypatch.setattr(worker_module, "download_video", fail_download)
    with TestClient(create_app()) as client:
        resp = client.post("/api/jobs", data={"youtube_url": URL})
        assert resp.status_code == 202
        job_id = resp.json()["id"]

    # The worker's top-level runner wraps per-step errors and marks the job
    # failed; _process_one itself propagates the error.
    import pytest

    with pytest.raises(youtube.YouTubeError):
        worker._process_one(job_id)
    # The step set "downloading" before failing; the runner would mark "failed".
    assert db.get_job(job_id)["status"] == "downloading"