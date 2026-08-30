from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.api.jobs as jobs_api
import app.core.config as config
from app.main import create_app
from app.repositories import database as db
from app.services.inference import inference_service
from app.services.worker import worker

from .test_database_pipeline import configure_temp_data


def test_create_read_pause_resume_job(monkeypatch, tmp_path: Path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs_api, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(inference_service, "start", lambda: None)
    monkeypatch.setattr(inference_service, "stop", lambda: None)
    monkeypatch.setattr(worker, "start", lambda: None)
    monkeypatch.setattr(worker, "stop", lambda: None)
    monkeypatch.setattr(worker, "wake", lambda: None)
    db.init_db(run_legacy_migration=False)
    audio = tmp_path / "voice-profiles" / "voice.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"wav")
    db.create_voice_profile(
        "voice", "เสียงทดสอบ", "ข้อความอ้างอิง", config.data_relative(audio), "hash", 1000, []
    )

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/jobs",
            data={"voice_profile_id": "voice", "pause_after_translation": "true"},
            files={"video": ("clip.mp4", b"not-probed-until-worker-runs", "video/mp4")},
        )
        assert response.status_code == 202
        created = response.json()
        assert created["status"] == "queued"
        assert created["pause_after_translation"] is True
        assert not any(key.endswith("_path") for key in created)
        job_id = created["id"]
        assert client.get(f"/api/jobs/{job_id}").status_code == 200
        assert client.post(f"/api/jobs/{job_id}/actions", json={"action": "pause"}).status_code == 202
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "paused"
        assert client.post(f"/api/jobs/{job_id}/actions", json={"action": "resume"}).status_code == 202

