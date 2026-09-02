from fastapi import APIRouter, HTTPException

from ..repositories import database
from ..services import edge_tts_synth

router = APIRouter(prefix="/api/voices", tags=["voices"])


@router.get("")
def voices() -> list[dict]:
    """List preset Edge TTS voices. Reaches the network to fetch the current catalogue."""
    try:
        raw = edge_tts_synth.list_voices()
    except edge_tts_synth.EdgeTTSUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    result = []
    for voice in raw:
        result.append(
            {
                "short_name": voice["ShortName"],
                "locale": voice.get("Locale") or "",
                "gender": voice.get("Gender") or "",
                "label": f"{voice['ShortName']} - {voice.get('Locale', '')} ({voice.get('Gender', '')})",
            }
        )
    result.sort(key=lambda item: item["short_name"])
    return result


@router.get("/default")
def default_voice() -> dict:
    settings = database.get_settings()
    return {"voice": settings["voice"], "tts_rate": settings.get("tts_rate", 0)}


@router.put("/default")
def set_default_voice(voice: str, tts_rate: int = 0) -> dict:
    settings = database.save_settings(
        database.get_settings()["max_start_delay_ms"], voice=voice, tts_rate=tts_rate
    )
    return {"voice": settings["voice"], "tts_rate": settings.get("tts_rate", 0)}