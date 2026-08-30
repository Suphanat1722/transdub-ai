from fastapi import HTTPException

from ..repositories import database


def require_job(job_id: str, include_cues: bool = True) -> dict:
    job = database.get_job(job_id, include_cues=include_cues)
    if not job:
        raise HTTPException(404, "ไม่พบงาน")
    return job


def require_profile(profile_id: str) -> dict:
    profile = database.get_voice_profile(profile_id)
    if not profile:
        raise HTTPException(404, "ไม่พบโปรไฟล์เสียง")
    return profile
