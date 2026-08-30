from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from ..core.config import JOBS_DIR, MAX_VIDEO_BYTES, resolve_data_path
from ..repositories import database as db
from ..services.translation import serialize_srt
from ..services.worker import worker
from .dependencies import require_job, require_profile
from .schemas import CueEditRequest, JobActionRequest

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
UPLOAD_CHUNK = 1024 * 1024
ACTIVE = {
    "running",
    "extracting",
    "separating",
    "transcribing",
    "translating",
    "synthesizing",
    "muxing",
}
TERMINAL = {"completed", "failed", "cancelled", "needs_review"}


def public_job(job: dict) -> dict:
    result = dict(job)
    for key in list(result):
        if key.endswith("_path"):
            result.pop(key, None)
    result["artifacts"] = [
        {**artifact, "download_url": f"/api/jobs/{job['id']}/artifacts/{artifact['kind']}"}
        for artifact in db.list_artifacts(job["id"])
    ]
    return result


async def _save_upload(upload: UploadFile, target: Path) -> None:
    written = 0
    try:
        with target.open("wb") as output:
            while chunk := await upload.read(UPLOAD_CHUNK):
                written += len(chunk)
                if written > MAX_VIDEO_BYTES:
                    raise HTTPException(413, "ไฟล์วิดีโอใหญ่เกินขนาดที่กำหนด")
                output.write(chunk)
    finally:
        await upload.close()
    if written == 0:
        raise HTTPException(422, "ไฟล์วิดีโอว่างเปล่า")


@router.get("")
def jobs() -> list[dict]:
    return [public_job(job) for job in db.list_jobs() if job.get("engine") == "transdub"]


@router.post("", status_code=202)
async def create_job(
    video: UploadFile = File(...),
    voice_profile_id: str = Form(...),
    source_language: str = Form("auto"),
    pause_after_transcription: bool = Form(False),
    pause_after_translation: bool = Form(False),
    background_volume: float = Form(100),
    voice_volume: float = Form(100),
) -> dict:
    require_profile(voice_profile_id)
    if not 0 <= background_volume <= 150 or not 0 <= voice_volume <= 150:
        raise HTTPException(422, "ระดับเสียงต้องอยู่ระหว่าง 0–150 เปอร์เซ็นต์")
    safe_name = Path(video.filename or "video.mp4").name
    suffix = Path(safe_name).suffix.lower()[:12] or ".video"
    job_id = str(uuid.uuid4())
    job_dir = JOBS_DIR / job_id
    source_dir = job_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=False)
    target = source_dir / f"video{suffix}"
    try:
        await _save_upload(video, target)
        created = db.create_video_job(
            job_id=job_id,
            filename=safe_name,
            source_path=target,
            voice_profile_id=voice_profile_id,
            source_language=source_language.strip()[:80] or "auto",
            pause_after_transcription=pause_after_transcription,
            pause_after_translation=pause_after_translation,
            background_volume=background_volume,
            voice_volume=voice_volume,
        )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    worker.wake()
    return public_job(created)


@router.get("/{job_id}")
def job_detail(job_id: str) -> dict:
    return public_job(require_job(job_id))


@router.get("/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    require_job(job_id, include_cues=False)

    async def stream():
        last = ""
        while True:
            job = require_job(job_id, include_cues=False)
            payload = json.dumps(public_job(job), ensure_ascii=False, default=str)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            else:
                yield ": keep-alive\n\n"
            if job["status"] in TERMINAL:
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/{job_id}/actions", status_code=202)
def action(job_id: str, request: JobActionRequest) -> dict:
    job = require_job(job_id, include_cues=False)
    selected = request.action
    if selected == "pause":
        if job["status"] in TERMINAL or job["status"].startswith("reviewing_"):
            raise HTTPException(409, "สถานะนี้ไม่สามารถหยุดชั่วคราวได้")
        if job["status"] in ACTIVE:
            db.update_job(job_id, control_requested="pause", wait_reason="กำลังจบหน่วยงานปัจจุบัน")
        else:
            db.update_job(job_id, status="paused", wait_reason="ผู้ใช้หยุดชั่วคราว")
    elif selected == "cancel":
        if job["status"] in TERMINAL:
            raise HTTPException(409, "งานนี้จบแล้ว")
        if job["status"] in ACTIVE:
            db.update_job(job_id, control_requested="cancel", wait_reason="กำลังจบหน่วยงานปัจจุบัน")
        else:
            db.update_job(job_id, status="cancelled", control_requested=None, wait_reason=None)
    elif selected in {"resume", "retry"}:
        if job["status"] not in {"paused", "failed", "waiting_quota", "needs_review"}:
            raise HTTPException(409, "สถานะนี้ไม่สามารถทำต่อได้")
        if selected == "retry":
            db.reset_failed_cues(job_id)
        db.update_job(
            job_id,
            status="queued",
            error=None,
            wait_reason=None,
            next_attempt_at=None,
            control_requested=None,
        )
        worker.wake()
    elif selected == "approve_transcript":
        if job["stage"] != "transcribed":
            raise HTTPException(409, "งานยังไม่อยู่ที่ขั้นตรวจ transcript")
        db.update_job(job_id, transcript_approved=1, status="queued", wait_reason=None)
        worker.wake()
    elif selected == "approve_translation":
        if job["stage"] != "translated":
            raise HTTPException(409, "งานยังไม่อยู่ที่ขั้นตรวจคำแปล")
        db.update_job(job_id, translation_approved=1, status="queued", wait_reason=None)
        worker.wake()
    elif selected == "remux":
        if not job.get("dub_audio_path"):
            raise HTTPException(409, "ยังไม่มีเสียงพากย์พร้อม remux")
        db.delete_artifacts(job_id, {"final_video"})
        db.update_job(job_id, stage="synthesized", status="queued", output_video_path=None)
        worker.wake()
    return public_job(require_job(job_id, include_cues=False))


@router.get("/{job_id}/cues")
def cues(
    job_id: str,
    layer: str = Query("translation", pattern="^(source|translation)$"),
    offset: int = 0,
    limit: int = 100,
) -> dict:
    require_job(job_id, include_cues=False)
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "offset/limit ไม่ถูกต้อง")
    if layer == "source":
        rows = db.source_cues(job_id)
        return {"items": rows[offset : offset + limit], "offset": offset, "limit": limit, "total": len(rows)}
    rows, total = db.list_cues(job_id, offset=offset, limit=limit)
    return {"items": rows, "offset": offset, "limit": limit, "total": total}


def _validate_timeline(rows: list[dict], cue_id: int, start_ms: int, end_ms: int) -> None:
    if end_ms <= start_ms:
        raise HTTPException(422, "เวลาจบต้องมากกว่าเวลาเริ่ม")
    index = next((i for i, cue in enumerate(rows) if cue["id"] == cue_id), -1)
    if index < 0:
        raise HTTPException(404, "ไม่พบ cue")
    if index > 0 and start_ms < rows[index - 1]["end_ms"]:
        raise HTTPException(422, "เวลาเริ่มทับ cue ก่อนหน้า")
    if index + 1 < len(rows) and end_ms > rows[index + 1]["start_ms"]:
        raise HTTPException(422, "เวลาจบทับ cue ถัดไป")


@router.patch("/{job_id}/cues/{cue_id}")
def edit_cue(job_id: str, cue_id: int, request: CueEditRequest) -> dict:
    job = require_job(job_id)
    if job["status"] in ACTIVE:
        raise HTTPException(409, "หยุดงานก่อนแก้ cue")
    text = request.text.strip()
    if request.layer == "source":
        rows = db.source_cues(job_id)
        _validate_timeline(rows, cue_id, request.start_ms, request.end_ms)
        updated = db.update_source_cue(
            cue_id, text=text, start_ms=request.start_ms, end_ms=request.end_ms
        )
        if not updated:
            raise HTTPException(404, "ไม่พบ source cue")
        source_srt = resolve_data_path(job["source_srt_path"])
        source_srt.write_text(serialize_srt(db.source_cues(job_id), bom=True), encoding="utf-8")
        work_translation = JOBS_DIR / job_id / "work" / "translation"
        for checkpoint in work_translation.glob("*.json") if work_translation.is_dir() else []:
            checkpoint.unlink(missing_ok=True)
        shutil.rmtree(JOBS_DIR / job_id / "cues", ignore_errors=True)
        with db.connect() as conn:
            conn.execute("DELETE FROM cues WHERE job_id=?", (job_id,))
            conn.execute("UPDATE translation_chunks SET status='pending',model=NULL,error=NULL WHERE job_id=?", (job_id,))
        db.delete_artifacts(
            job_id,
            {"translated_srt", "dub_wav", "dub_mp3", "report_json", "report_csv", "final_video"},
        )
        db.update_job(
            job_id,
            stage="transcribed",
            status="reviewing_transcript" if job.get("pause_after_transcription") else "queued",
            transcript_approved=0 if job.get("pause_after_transcription") else 1,
            translation_approved=0,
            translated_srt_path=None,
            dub_audio_path=None,
            output_video_path=None,
            progress=45,
        )
    else:
        rows = job["cues"]
        _validate_timeline(rows, cue_id, request.start_ms, request.end_ms)
        target = next((cue for cue in rows if cue["id"] == cue_id), None)
        if not target:
            raise HTTPException(404, "ไม่พบ translated cue")
        dirty_ids = {cue_id}
        if target["start_ms"] != request.start_ms or target["end_ms"] != request.end_ms:
            previous = next((cue for cue in rows if cue["position"] == target["position"] - 1), None)
            if previous:
                dirty_ids.add(previous["id"])
        for dirty_id in dirty_ids:
            dirty = db.get_cue(dirty_id)
            if dirty and dirty.get("audio_path"):
                resolve_data_path(dirty["audio_path"]).unlink(missing_ok=True)
            db.update_cue(
                dirty_id,
                status="pending",
                audio_path=None,
                original_duration_ms=None,
                final_duration_ms=None,
                error=None,
                generation_revision=int((dirty or {}).get("generation_revision") or 0) + 1,
            )
        db.update_cue(cue_id, text=text, start_ms=request.start_ms, end_ms=request.end_ms)
        refreshed = db.get_job(job_id)
        if refreshed is None:
            raise HTTPException(404, "ไม่พบงาน")
        translated = refreshed["cues"]
        translated_srt = resolve_data_path(job["translated_srt_path"])
        translated_srt.write_text(serialize_srt(translated, bom=True), encoding="utf-8")
        db.delete_artifacts(job_id, {"dub_wav", "dub_mp3", "report_json", "report_csv", "final_video"})
        db.update_job(
            job_id,
            stage="translated",
            status=(
                "needs_review"
                if job["status"] == "needs_review"
                else "reviewing_translation"
                if job.get("pause_after_translation") and not job.get("translation_approved")
                else "queued"
            ),
            dub_audio_path=None,
            output_video_path=None,
            progress=55,
        )
    updated_job = require_job(job_id)
    if updated_job["status"] == "queued":
        worker.wake()
    return public_job(updated_job)


@router.get("/{job_id}/artifacts/{kind}")
def artifact(job_id: str, kind: str) -> FileResponse:
    job = require_job(job_id, include_cues=False)
    item = db.get_artifact(job_id, kind)
    if not item:
        raise HTTPException(404, "ไม่พบไฟล์ผลลัพธ์นี้")
    path = Path(item["resolved_path"])
    if not path.is_file() or not path.is_relative_to((JOBS_DIR / job_id).resolve()):
        raise HTTPException(404, "ไฟล์ผลลัพธ์หายหรือเส้นทางไม่ถูกต้อง")
    names = {
        "source_srt": f"{Path(job['filename']).stem}.source.srt",
        "translated_srt": f"{Path(job['filename']).stem}.th.srt",
        "dub_wav": f"{Path(job['filename']).stem}.th.wav",
        "dub_mp3": f"{Path(job['filename']).stem}.th.mp3",
        "final_video": f"{Path(job['filename']).stem}.th-dub.mp4",
    }
    return FileResponse(path, media_type=item["media_type"], filename=names.get(kind, path.name))


@router.get("/{job_id}/cues/{cue_id}/audio")
def cue_audio(job_id: str, cue_id: int) -> FileResponse:
    job = require_job(job_id)
    cue = next((item for item in job["cues"] if item["id"] == cue_id), None)
    if not cue or cue["status"] != "completed" or not cue.get("audio_path"):
        raise HTTPException(404, "เสียง cue ยังไม่พร้อม")
    path = resolve_data_path(cue["audio_path"])
    if not path.is_file() or not path.is_relative_to((JOBS_DIR / job_id).resolve()):
        raise HTTPException(404, "ไม่พบเสียง cue")
    return FileResponse(path, media_type="audio/wav")


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str) -> Response:
    job = require_job(job_id, include_cues=False)
    if job["status"] in ACTIVE:
        raise HTTPException(409, "หยุดงานก่อนลบ")
    job_dir = (JOBS_DIR / job_id).resolve()
    if not job_dir.is_relative_to(JOBS_DIR.resolve()):
        raise HTTPException(400, "เส้นทางงานไม่ถูกต้อง")
    staged = JOBS_DIR / f".deleting-{job_id}-{uuid.uuid4().hex}"
    if job_dir.exists():
        job_dir.rename(staged)
    try:
        if not db.delete_job(job_id):
            raise RuntimeError("ไม่พบงานในฐานข้อมูล")
    except Exception:
        if staged.exists():
            staged.rename(job_dir)
        raise
    shutil.rmtree(staged, ignore_errors=True)
    return Response(status_code=204)
