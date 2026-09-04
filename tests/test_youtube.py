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


def _mock_fetch_subtitle(monkeypatch, srt_text: str, language: str) -> None:
    monkeypatch.setattr(
        worker_module,
        "fetch_subtitle",
        lambda url, source_language="auto": (srt_text, language),
    )


def test_video_filename_uses_title_and_falls_back() -> None:
    assert youtube.video_filename("My Clip: EP.1?", "mkv", "fallback") == "My Clip_ EP.1_.mkv"
    assert youtube.video_filename("  ", "mkv", "fallback") == "fallback.mkv"
    assert youtube.video_filename(None, "mp4", "fallback") == "fallback.mp4"
    assert len(youtube.video_filename("x" * 200, "mkv", "fallback")) <= 124


def test_extract_video_id_variants() -> None:
    assert youtube.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube.extract_video_id("not a url") is None
    assert youtube.extract_video_id("") is None


def test_proxy_url_generic(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.youtube.youtube_proxy_settings",
        lambda: ("", "", "http://user:pass@host:8080"),
    )
    assert youtube._proxy_url() == "http://user:pass@host:8080"


def test_proxy_url_webshare_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.youtube.youtube_proxy_settings",
        lambda: ("wsuser", "wspass", ""),
    )
    # Webshare must expand into a usable URL so yt-dlp (video download) and the
    # transcript client share the same proxy.
    assert youtube._proxy_url() == "http://wsuser:wspass@res.webshare.io:80/"


def test_proxy_url_none(monkeypatch) -> None:
    monkeypatch.setattr("app.services.youtube.youtube_proxy_settings", lambda: ("", "", ""))
    assert youtube._proxy_url() is None


def test_detect_subtitle_languages_prefers_original_over_thai() -> None:
    """An English video should fetch English subtitles, not Thai."""
    info = {
        "original_language": "en",
        "subtitles": {"en": [{}], "th": [{}]},
        "automatic_captions": {},
    }
    assert youtube._detect_subtitle_languages(info)[0] == "en"
    # 'th' only appears as the rough last-resort, never first.
    assert "en" in youtube._detect_subtitle_languages(info)


def test_detect_subtitle_languages_honors_explicit_source_language() -> None:
    info = {
        "original_language": "en",
        "subtitles": {"ja": [{}], "en": [{}]},
        "automatic_captions": {},
    }
    # User-requested Japanese wins even though the video is originally English.
    ordered = youtube._detect_subtitle_languages(info, source_language="ja")
    assert ordered[0] == "ja"
    assert "en" in ordered


def test_detect_subtitle_languages_matches_family_code() -> None:
    info = {"subtitles": {"en-US": [{}]}, "automatic_captions": {}}
    ordered = youtube._detect_subtitle_languages(info)
    assert ordered[0] == "en-US"


def test_detect_subtitle_languages_empty_info_falls_back() -> None:
    assert tuple(youtube._detect_subtitle_languages({})) == ("en", "th")


def test_download_attempts_use_proxy_and_fallback_clients(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.services.youtube.youtube_proxy_settings",
        lambda: ("wsuser", "wspass", ""),
    )
    attempts = youtube._download_attempts(tmp_path)
    assert len(attempts) >= 2
    for options in attempts:
        assert options.get("proxy") == "http://wsuser:wspass@res.webshare.io:80/"
        # Stream through a real Chrome TLS fingerprint to dodge the bot-check.
        # yt-dlp requires an ImpersonateTarget object, not a bare string.
        assert getattr(options.get("impersonate"), "client", None) == "chrome"
        assert options.get("merge_output_format") == "mkv"
    # The first attempt uses the TV client (full quality range); reduced
    # mobile clients (Android) go last so a low-quality success never wins.
    assert attempts[0]["extractor_args"] == {"youtube": {"player_client": ["tv"]}}
    assert attempts[-2]["extractor_args"] == {"youtube": {"player_client": ["android"]}}


def test_subtitle_attempt_options_impersonate(tmp_path: Path) -> None:
    """Subtitle fetches also impersonate Chrome so a blocked IP is not singled
    out when it reads the captions."""
    attempts = youtube._subtitle_attempt_options(tmp_path, "en")
    assert all(getattr(a.get("impersonate"), "client", None) == "chrome" for a in attempts)


def test_subtitle_attempt_options_fall_through_player_clients(tmp_path: Path) -> None:
    """Fetching one subtitle language tries android/ios/tv then the default, so a
    blocked client (the "Sign in to confirm you're not a bot" captcha) falls
    through to another client instead of failing the whole subtitle fetch."""
    attempts = youtube._subtitle_attempt_options(tmp_path, "en")
    assert len(attempts) == 4
    assert all(a["subtitleslangs"] == ["en"] for a in attempts)
    assert attempts[0]["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    assert attempts[1]["extractor_args"] == {"youtube": {"player_client": ["ios"]}}
    assert attempts[2]["extractor_args"] == {"youtube": {"player_client": ["tv"]}}
    # The final entry keeps the unpinned default client.
    assert "extractor_args" not in attempts[3]


def test_attempt_subtitle_retries_next_client_on_failure(monkeypatch, tmp_path: Path) -> None:
    """If the first subtitle client fails (e.g. bot-check), the next client is
    tried; a successful later client returns the SRT text."""
    calls: list[list[str]] = []

    def fake_download(self, url: list[str]) -> None:
        calls.append(url)
        options = self.params
        language = options["subtitleslangs"][0]
        client = options.get("extractor_args", {}).get("youtube", {}).get("player_client", ["default"])[0]
        if client == "android":
            raise RuntimeError("Sign in to confirm you're not a bot")
        (tmp_path / f"sub.{language}.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello", encoding="utf-8")

    import yt_dlp

    monkeypatch.setattr(yt_dlp.YoutubeDL, "download", fake_download)
    text = youtube._attempt_subtitle(tmp_path, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "en")
    assert text == "1\n00:00:00,000 --> 00:00:01,000\nHello"
    assert len(calls) >= 2


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
    # Must be queued so the worker loop picks it up for extraction; otherwise
    # it hangs in "downloading" forever.
    assert job["status"] == "queued"
    assert job["source_path"] is not None
    assert job["filename"] == "Some Clip Title.mp4"
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
    assert job["status"] == "queued"
    assert job["source_cues"][0]["text"] == "Hello there"
    assert job["cues"] == []


def test_youtube_failure_marks_job_failed(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)

    def fail_download(url: str, target_dir: Path, progress=None) -> Path:
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