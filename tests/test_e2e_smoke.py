"""Phase-3 smoke tests: ready/queue/logs/validate endpoints, wizard UI, helpers.

No network (no YouTube/Gemini/Edge TTS calls); uses temp data dirs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
from app.core.config import ROOT
from app.main import create_app
from app.repositories import database as db
from app.services.translation import affected_chunk_ids_for_source_index
from app.services.worker import unique_export_path, worker

from .test_database_pipeline import configure_temp_data


def _client(monkeypatch, tmp_path: Path) -> TestClient:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db()
    return TestClient(create_app())


def test_ready_validate_and_bad_url(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert {"ffmpeg_available", "gemini_configured"} <= set(ready.json())

    empty = client.post("/api/jobs/validate-folder", json={"path": ""})
    assert empty.status_code == 200
    assert empty.json()["ok"] is True

    bad = client.post("/api/jobs/validate-folder", json={"path": "Z:/no-such-dir-xyz-123"})
    assert bad.status_code == 422

    rejected = client.post("/api/jobs", data={"youtube_url": "https://example.com/ab"})
    assert rejected.status_code == 422


def test_create_fast_job_queue_and_logs(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/jobs",
        data={
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "voice": "th-TH-NiwatNeural",
            "separation_mode": "fast",
        },
    )
    assert response.status_code == 202
    created = response.json()
    assert created["separation_mode"] == "fast"
    job_id = created["id"]

    queue = client.get(f"/api/jobs/{job_id}/queue")
    assert queue.status_code == 200
    assert queue.json()["queued"] >= 1

    logs = client.get(f"/api/jobs/{job_id}/logs")
    assert logs.status_code == 200
    body = logs.json()
    assert body["separation_mode"] == "fast"
    assert "attempts" in body and "usage" in body


def test_affected_chunk_helper_and_static_wizard(monkeypatch, tmp_path: Path) -> None:
    _client(monkeypatch, tmp_path)
    db.create_video_job(
        job_id="smoke", filename="v.mp4", source_path="https://x", source_language="en",
        pause_after_transcription=False, pause_after_translation=False,
        background_volume=100, voice_volume=100,
    )
    db.replace_translation_chunks(
        "smoke",
        [
            {"id": "c0", "index": 0, "target_start": 0, "target_end": 4,
             "context_start": 0, "context_end": 5},
            {"id": "c1", "index": 1, "target_start": 5, "target_end": 9,
             "context_start": 4, "context_end": 9},
        ],
    )
    assert affected_chunk_ids_for_source_index("smoke", 2) == ["c0"]
    assert affected_chunk_ids_for_source_index("smoke", 7) == ["c1"]
    assert affected_chunk_ids_for_source_index("smoke", 99) == []

    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="create-steps"' in html
    assert 'id="confirm-modal"' in html
    assert 'id="logs-card"' in html
    assert 'name="separation_mode"' in html

    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "confirmModal" in js
    assert "saveAllCues" in js
    assert "withPreservedCues" in js
    assert "affected" not in js  # helper stays backend-only
    assert "/queue" in js


def test_reassemble_action_requeues_finished_job(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/jobs",
        data={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert response.status_code == 202
    job_id = response.json()["id"]

    # Fresh job has nothing to reassemble.
    assert client.post(f"/api/jobs/{job_id}/actions", json={"action": "reassemble"}).status_code == 409

    db.replace_translation_cues(job_id, [
        {"start_ms": 0, "end_ms": 1000, "text": "สวัสดี", "source_cue_indexes": [1],
         "translation_chunk_id": "c0", "warnings": []}
    ])
    db.update_job(job_id, stage="completed", status="completed")
    retry = client.post(f"/api/jobs/{job_id}/actions", json={"action": "reassemble"})
    assert retry.status_code == 202
    body = retry.json()
    assert body["stage"] == "synthesizing"
    assert body["status"] == "queued"


def test_unique_export_path_avoids_collision(tmp_path: Path) -> None:
    assert unique_export_path(tmp_path, "clip.th-dub", ".mp4") == tmp_path / "clip.th-dub.mp4"
    (tmp_path / "clip.th-dub.mp4").write_bytes(b"x")
    assert unique_export_path(tmp_path, "clip.th-dub", ".mp4") == tmp_path / "clip.th-dub -2.mp4"
    (tmp_path / "clip.th-dub -2.mp4").write_bytes(b"x")
    assert unique_export_path(tmp_path, "clip.th-dub", ".mp4") == tmp_path / "clip.th-dub -3.mp4"
