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
    """Return the current .env entries as a dict of key=value."""
    if not (ROOT / ".env").is_file():
        return {}
    values: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@router.get("/api-key")
def get_api_key() -> dict:
    """Report whether a Gemini API key is configured (never the key itself)."""
    return {"configured": bool(os.getenv("GEMINI_API_KEY", "").strip())}


@router.put("/api-key")
def set_api_key(request: ApiKeyRequest) -> dict:
    """Store the Gemini API key in .env (and apply it to this process immediately)."""
    key = request.api_key.strip()
    if not key:
        raise HTTPException(422, "กรุณาใส่ API key")
    values = _read_env()
    values["GEMINI_API_KEY"] = key
    lines = [f"{k}={v}" for k, v in values.items()]
    os.makedirs(ROOT, exist_ok=True)
    (ROOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = key
    return {"configured": True}