from fastapi import APIRouter

from ..repositories import database
from .schemas import LocalSettings

router = APIRouter(prefix="/api/settings/local", tags=["settings"])


@router.get("", response_model=LocalSettings)
def get_local_settings() -> dict:
    return database.get_settings()


@router.put("", response_model=LocalSettings)
def put_local_settings(settings: LocalSettings) -> dict:
    return database.save_settings(
        settings.nfe_step,
        settings.inference_speed,
        settings.max_start_delay_ms,
        settings.allow_cpu,
    )
