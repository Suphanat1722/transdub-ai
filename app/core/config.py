from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = (ROOT / "data").resolve()
JOBS_DIR = DATA_DIR / "jobs"
CACHE_DIR = DATA_DIR / "cache"
# Finished videos go here when the job has no custom output folder.
OUTPUTS_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "app.db"
STATIC_DIR = ROOT / "app" / "static"

EDGE_TTS_DEFAULT_VOICE = "th-TH-NiwatNeural"
TRANSLATION_MODELS = tuple(
    value.strip()
    for value in os.getenv(
        "TRANSLATION_MODELS",
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite",
    ).split(",")
    if value.strip()
)
HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8765"))
SAMPLE_RATE = 24_000
SAMPLE_WIDTH = 2
CHANNELS = 1
# Cap for speeding a whole speech group to fit its subtitle window.
GROUP_MAX_SPEED = 1.25
# Floor for slowing a whole speech group to fill its subtitle window.
GROUP_MIN_SPEED = 0.8
# Subtitle gap (ms) at or below which cues belong to one speech group and
# share a single uniform fit speed.  Larger gaps are real pauses: kept as-is.
GROUP_CONTIGUITY_MS = 400
# Edge TTS cues synthesized concurrently per worker pass. Each cue is claimed
# as `processing` before submission so restarts never double-synth one.
TTS_SYNTH_WORKERS = 4
CACHE_FORMAT_REVISION = "edge-tts-v1"
CACHE_MAX_AGE_DAYS = 30
CACHE_MAX_BYTES = 10 * 1024**3
PIPELINE_REVISION = "transdub-edge-v1"
ASSEMBLY_STEM_SIZE = 64
MAX_SUBPROCESS_COMMAND_CHARS = 20_000


def ensure_directories() -> None:
    for path in (DATA_DIR, JOBS_DIR, CACHE_DIR, OUTPUTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def youtube_proxy_settings() -> tuple[str, str, str]:
    """Return (webshare_user, webshare_pass, generic_proxy_url) from the env.

    Lets the YouTube transcript client route through a residential proxy when
    YouTube blocks the local/cloud IP (same approach as the reference
    extractor).  Empty strings mean "no proxy configured".
    """
    return (
        os.getenv("YOUTUBE_PROXY_WEBSHARE_USER", "").strip(),
        os.getenv("YOUTUBE_PROXY_WEBSHARE_PASS", "").strip(),
        os.getenv("YOUTUBE_PROXY_URL", "").strip(),
    )


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def data_relative(path: str | Path) -> str:
    """Store portable paths and reject anything outside the data directory."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(DATA_DIR):
        raise ValueError(f"Path is outside data directory: {resolved}")
    return resolved.relative_to(DATA_DIR).as_posix()


def resolve_data_path(path: str | Path) -> Path:
    """Resolve a database path without permitting traversal outside data/."""
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else DATA_DIR / candidate).resolve()
    if not resolved.is_relative_to(DATA_DIR):
        raise ValueError(f"Unsafe data path: {path}")
    return resolved
