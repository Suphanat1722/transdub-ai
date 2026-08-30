import importlib.util

from fastapi import APIRouter

from ..core.config import TRANSCRIPTION_MODEL, TRANSLATION_MODELS, ffmpeg_path, gemini_api_key
from ..services.inference import inference_service

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health() -> dict:
    status = inference_service.status()
    return {
        "ok": bool(ffmpeg_path()) and status.get("state") != "error",
        "ffmpeg_available": bool(ffmpeg_path()),
        "gemini_configured": bool(gemini_api_key()),
        "demucs_available": importlib.util.find_spec("demucs") is not None,
        "model": status,
        "transcription_model": TRANSCRIPTION_MODEL,
        "translation_models": TRANSLATION_MODELS,
        "engine": "transdub-ai",
    }


@router.get("/model/status")
def model_status() -> dict:
    return inference_service.status()
