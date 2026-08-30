import shutil
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from ..core.config import ALLOWED_REFERENCE_EXTENSIONS, PROFILES_DIR, data_relative, resolve_data_path
from ..repositories import database
from ..services.audio import AudioError, normalize_reference
from .dependencies import require_profile

router = APIRouter(prefix="/api/voice-profiles", tags=["voice profiles"])
MAX_REFERENCE_BYTES = 50 * 1024 * 1024


@router.get("")
def voice_profiles() -> list[dict]:
    return database.list_voice_profiles()


@router.post("", status_code=201)
async def create_voice_profile(
    name: str = Form(..., min_length=1, max_length=80),
    transcript: str = Form(..., min_length=1, max_length=2000),
    file: UploadFile = File(...),
) -> dict:
    extension = Path(file.filename or "").suffix.lower()
    if extension not in ALLOWED_REFERENCE_EXTENSIONS:
        raise HTTPException(422, "รองรับเฉพาะ WAV, MP3, M4A, FLAC และ OGG")
    raw = await file.read(MAX_REFERENCE_BYTES + 1)
    if len(raw) > MAX_REFERENCE_BYTES:
        raise HTTPException(413, "ไฟล์เสียงใหญ่เกิน 50 MB")
    profile_id = str(uuid.uuid4())
    profile_dir = PROFILES_DIR / profile_id
    profile_dir.mkdir(parents=True)
    source = profile_dir / f"source{extension}"
    normalized = profile_dir / "reference.wav"
    source.write_bytes(raw)
    try:
        duration, digest, warnings = normalize_reference(source, normalized)
        return database.create_voice_profile(
            profile_id,
            name.strip(),
            transcript.strip(),
            data_relative(normalized),
            digest,
            duration,
            warnings,
        )
    except sqlite3.IntegrityError as exc:
        shutil.rmtree(profile_dir)
        raise HTTPException(409, "ชื่อโปรไฟล์นี้มีอยู่แล้ว") from exc
    except AudioError as exc:
        shutil.rmtree(profile_dir)
        raise HTTPException(422, str(exc)) from exc


@router.get("/{profile_id}/audio")
def voice_profile_audio(profile_id: str) -> FileResponse:
    profile = require_profile(profile_id)
    path = resolve_data_path(profile["audio_path"])
    if not path.is_relative_to((PROFILES_DIR / profile_id).resolve()):
        raise HTTPException(400, "เส้นทางเสียงอ้างอิงไม่ถูกต้อง")
    if not path.is_file():
        raise HTTPException(404, "ไม่พบไฟล์เสียงอ้างอิง")
    return FileResponse(path, media_type="audio/wav", filename=f"{profile['name']}.wav")


@router.delete("/{profile_id}", status_code=204)
def delete_voice_profile(profile_id: str) -> Response:
    require_profile(profile_id)
    try:
        path = database.delete_voice_profile(profile_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if path:
        profile_dir = resolve_data_path(path).parent
        if profile_dir.is_dir() and profile_dir.is_relative_to(PROFILES_DIR.resolve()):
            shutil.rmtree(profile_dir)
    return Response(status_code=204)
