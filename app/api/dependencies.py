from fastapi import HTTPException

from ..repositories import database


def require_job(job_id: str, include_cues: bool = True) -> dict:
    job = database.get_job(job_id, include_cues=include_cues)
    if not job:
        raise HTTPException(404, "ไม่พบงาน")
    return job
