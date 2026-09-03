import os

from fastapi import APIRouter, HTTPException

from ..core.config import ROOT
from ..repositories import database
from .schemas import ApiKeyRequest, LocalSettings

router = APIRouter(prefix="/api/settings/local", tags=["settings"])


@router.get("", response_model=LocalSettings)
def get_local_settings() -> dict:
    return database.get_settings()


@router.put("", response_model=LocalSettings)
def put_local_settings(settings: LocalSettings) -> dict:
    return database.save_settings(
        settings.max_start_delay_ms, voice=settings.voice, tts_rate=settings.tts_rate
    )


def _read_env() -> dict[str, str]:
    """Return the current .env entries (handles quotes/= via python-dotenv)."""
    from dotenv import dotenv_values

    env_file = ROOT / ".env"
    if not env_file.is_file():
        return {}
    return {k: v for k, v in dotenv_values(env_file).items() if v is not None}


@router.get("/api-key")
def get_api_key() -> dict:
    """Report whether a Gemini API key is configured (never the key itself)."""
    return {"configured": bool(os.getenv("GEMINI_API_KEY", "").strip())}


@router.put("/api-key")
def set_api_key(request: ApiKeyRequest) -> dict:
    """Store the Gemini API key in .env (and apply it to this process immediately)."""
    from dotenv import set_key

    key = request.api_key.strip()
    if not key:
        raise HTTPException(422, "กรุณาใส่ API key")
    env_file = ROOT / ".env"
    os.makedirs(ROOT, exist_ok=True)
    if not env_file.is_file():
        env_file.touch()
    set_key(str(env_file), "GEMINI_API_KEY", key)
    os.environ["GEMINI_API_KEY"] = key
    return {"configured": True}