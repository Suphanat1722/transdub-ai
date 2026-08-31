from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA_DIR = (ROOT / "data").resolve()
JOBS_DIR = DATA_DIR / "jobs"
PROFILES_DIR = DATA_DIR / "voice-profiles"
IMPORTS_DIR = DATA_DIR / "imports"
CACHE_DIR = DATA_DIR / "cache"
HF_CACHE_DIR = DATA_DIR / "hf-cache"
MEDIA_CACHE_DIR = DATA_DIR / "media-cache"
DB_PATH = DATA_DIR / "app.db"
MODEL_STATUS_PATH = DATA_DIR / "model-status.json"
STATIC_DIR = ROOT / "app" / "static"
FLOWTTS_ROOT = ROOT / "vendor" / "thonburian-tts"

MODEL_NAME = "JTS-AI/JaiTTS-F5TTS"
TRANSCRIPTION_MODEL = os.getenv("TRANSCRIPTION_MODEL", "gemini-3.5-transcribe")
TRANSLATION_MODELS = tuple(
    value.strip()
    for value in os.getenv(
        "TRANSLATION_MODELS",
        "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash-lite",
    ).split(",")
    if value.strip()
)
HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8765"))
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(8 * 1024**3)))
MODEL_LOCAL_ROOTS = (
    ROOT / "models" / "JaiTTS-F5TTS",
    ROOT / "models--JTS-AI--JaiTTS-F5TTS",
)


def _find_local_model_snapshot() -> Path | None:
    candidates: list[Path] = []
    for model_root in MODEL_LOCAL_ROOTS:
        candidates.append(model_root)
        snapshots = model_root / "snapshots"
        if snapshots.is_dir():
            candidates.extend(
                sorted(
                    (path for path in snapshots.iterdir() if path.is_dir()),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            )
    for candidate in candidates:
        if (candidate / "model.pt").is_file() and (candidate / "vocab.txt").is_file():
            return candidate
    return None


MODEL_LOCAL_SNAPSHOT = _find_local_model_snapshot()
MODEL_REVISION = MODEL_LOCAL_SNAPSHOT.name if MODEL_LOCAL_SNAPSHOT else "not-installed"
MODEL_CHECKPOINT = str(MODEL_LOCAL_SNAPSHOT / "model.pt") if MODEL_LOCAL_SNAPSHOT else ""
MODEL_VOCAB = str(MODEL_LOCAL_SNAPSHOT / "vocab.txt") if MODEL_LOCAL_SNAPSHOT else ""
MODEL_SOURCE = "local"
FLOWTTS_COMMIT = "032fe7e51674afe066a98e6d3cf47fc96d04b290"

SAMPLE_RATE = 24_000
SAMPLE_WIDTH = 2
CHANNELS = 1
MAX_SPEED = 1.25
MAX_END_SPEED = 1.5
CACHE_FORMAT_REVISION = "jaitts-batch-duration-v8"
CACHE_MAX_AGE_DAYS = 30
CACHE_MAX_BYTES = 10 * 1024**3
PIPELINE_REVISION = "transdub-v1"
AUTOMATIC_DURATION_MULTIPLIERS = (1.10, 1.35)
MANUAL_LONG_DURATION_MULTIPLIER = 1.65
ASSEMBLY_STEM_SIZE = 64
MAX_SUBPROCESS_COMMAND_CHARS = 20_000
ALLOWED_REFERENCE_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg"}


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        JOBS_DIR,
        PROFILES_DIR,
        IMPORTS_DIR,
        CACHE_DIR,
        HF_CACHE_DIR,
        MEDIA_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def gemini_api_key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


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
