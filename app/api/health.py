import importlib.util

from fastapi import APIRouter

from ..core.config import (
    EDGE_TTS_DEFAULT_VOICE,
    TRANSCRIPTION_MODEL,
    TRANSLATION_MODELS,
    ffmpeg_path,
    gemini_api_key,
)
from ..services import edge_tts_synth

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    edge_ok = _edge_tts_available()
    return {
        "ok": bool(ffmpeg_path()) and edge_ok,
        "ffmpeg_available": bool(ffmpeg_path()),
        "gemini_configured": bool(gemini_api_key()),
        "demucs_available": importlib.util.find_spec("demucs") is not None,
        "edge_tts_available": edge_ok,
        "default_voice": EDGE_TTS_DEFAULT_VOICE,
        "transcription_model": TRANSCRIPTION_MODEL,
        "translation_models": TRANSLATION_MODELS,
        "engine": "transdub-edge",
    }


def _edge_tts_available() -> bool:
    try:
        edge_tts_synth.list_voices()
        return True
    except edge_tts_synth.EdgeTTSUnavailableError:
        return False


@router.get("/model/status")
def model_status() -> dict:
    """Edge TTS needs no local model; report package and service availability."""
    if importlib.util.find_spec("edge_tts") is None:
        return {"state": "error", "error": "ยังไม่ได้ติดตั้ง edge-tts"}
    try:
        voices = edge_tts_synth.list_voices()
        return {"state": "ready", "service": "edge-tts", "voice_count": len(voices)}
    except edge_tts_synth.EdgeTTSUnavailableError as exc:
        return {"state": "error", "error": str(exc)}