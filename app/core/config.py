from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = (ROOT / "data").resolve()
JOBS_DIR = DATA_DIR / "jobs"
IMPORTS_DIR = DATA_DIR / "imports"
CACHE_DIR = DATA_DIR / "cache"
MEDIA_CACHE_DIR = DATA_DIR / "media-cache"
DB_PATH = DATA_DIR / "app.db"
STATIC_DIR = ROOT / "app" / "static"

EDGE_TTS_DEFAULT_VOICE = "th-TH-NiwatNeural"
# Edge TTS rate is raised in these steps (percent) until a cue fits its slot
# without cutting words, capped at MAX_TTS_RATE.
RATE_STEP = 10
MAX_TTS_RATE = 50
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
MAX_SPEED = 1.25
CACHE_FORMAT_REVISION = "edge-tts-v1"
CACHE_MAX_AGE_DAYS = 30
CACHE_MAX_BYTES = 10 * 1024**3
PIPELINE_REVISION = "transdub-edge-v1"
ASSEMBLY_STEM_SIZE = 64
MAX_SUBPROCESS_COMMAND_CHARS = 20_000


def ensure_directories() -> None:
    for path in (DATA_DIR, JOBS_DIR, IMPORTS_DIR, CACHE_DIR, MEDIA_CACHE_DIR):
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
