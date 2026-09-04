from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core.config import (
    CACHE_DIR,
    CACHE_FORMAT_REVISION,
    EDGE_TTS_DEFAULT_VOICE,
    JOBS_DIR,
    OUTPUTS_DIR,
    PIPELINE_REVISION,
    TTS_SYNTH_WORKERS,
    data_relative,
    resolve_data_path,
)
from ..repositories import database as db
from .audio import (
    assemble,
    trim_edge_silence,
    write_report,
)
from .edge_tts_synth import synth_cue
from .media import (
    MediaError,
    SeparationCancelled,
    create_background_stem,
    extract_original_audio,
    mix_output,
    parse_demucs_progress,
    probe_media,
)
from .srt import SrtValidationError, parse_srt
from .translation import QuotaWait, TranslationError, serialize_srt, translate
from .youtube import YouTubeError, download_video, fetch_subtitle, video_filename

logger = logging.getLogger(__name__)


def unique_export_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a non-existing path, appending `` -2``, `` -3``, ... on collision.

    Re-running the same clip (or two clips with one title) must never silently
    overwrite a previous export.
    """
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem} -{index}{suffix}"
        index += 1
    return candidate


class JobWorker:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name="transdub-worker")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def wake(self) -> None:
        self.wake_event.set()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            job_id = self._claim_job()
            if not job_id:
                self.wake_event.wait(1)
                self.wake_event.clear()
                continue
            try:
                self._process_one(job_id)
            except QuotaWait as exc:
                current = db.get_job(job_id, include_cues=False) or {}
                db.update_job(
                    job_id,
                    status="waiting_quota",
                    wait_reason=str(exc),
                    next_attempt_at=exc.retry_at.isoformat(),
                    quota_retries=int(current.get("quota_retries") or 0) + 1,
                )
            except Exception as exc:
                logger.exception("Pipeline job failed")
                db.record_attempt(job_id, "pipeline", "error", message=str(exc)[-1200:])
                db.update_job(
                    job_id,
                    status="failed",
                    error=str(exc)[-1200:],
                    wait_reason=None,
                    current_cue_id=None,
                )

    def _claim_job(self) -> str | None:
        now = db.utc_now()
        with db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='queued',wait_reason=NULL,next_attempt_at=NULL,updated_at=? "
                "WHERE engine='transdub' AND status='waiting_quota' AND next_attempt_at<=?",
                (now, now),
            )
            row = conn.execute(
                "SELECT id FROM jobs WHERE engine='transdub' AND status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE jobs SET status='running',started_at=COALESCE(started_at,?),updated_at=? "
                "WHERE id=? AND status='queued'",
                (now, now, row["id"]),
            ).rowcount
            return row["id"] if changed else None

    def _handle_control(self, job_id: str) -> bool:
        job = db.get_job(job_id, include_cues=False)
        if not job:
            return True
        control = job.get("control_requested")
        if control == "cancel":
            db.update_job(
                job_id, status="cancelled", control_requested=None, current_cue_id=None, wait_reason=None
            )
            return True
        if control == "pause":
            db.update_job(
                job_id,
                status="paused",
                control_requested=None,
                current_cue_id=None,
                wait_reason="ผู้ใช้หยุดชั่วคราว",
            )
            return True
        return False

    def _process_one(self, job_id: str) -> None:
        if self._handle_control(job_id):
            return
        job = db.get_job(job_id)
        if not job:
            return
        stage = job.get("stage") or "uploaded"
        if stage == "uploaded":
            self._download_youtube(job)
        elif stage == "downloaded":
            self._extract(job)
        elif stage == "extracted":
            self._separate(job)
        elif stage == "separated":
            if job.get("mode") == "import":
                # Thai subtitle: used as the dub text directly; Gemini skipped.
                self._skip_to_synthesize(job)
            else:
                # Non-Thai subtitle: source cues are populated; Gemini translates.
                self._import_pending_to_translate(job)
        elif stage == "transcribed":
            if job.get("pause_after_transcription") and not job.get("transcript_approved"):
                db.update_job(job_id, status="reviewing_transcript", progress=45)
            else:
                self._translate(job)
        elif stage == "translated":
            if job.get("pause_after_translation") and not job.get("translation_approved"):
                db.update_job(job_id, status="reviewing_translation", progress=55)
            else:
                self._synthesize(job)
        elif stage == "synthesizing":
            self._synthesize(job)
        elif stage == "synthesized":
            self._mux(job)
        else:
            raise RuntimeError(f"ไม่รู้จัก pipeline stage: {stage}")

    def _download_youtube(self, job: dict) -> None:
        job_id = job["id"]
        url = (job.get("source_path") or "").strip()
        if not url:
            raise RuntimeError("งาน YouTube ไม่มี URL ต้นฉบับ")
        db.update_job(job_id, status="downloading", progress=2, error=None, wait_reason=None)
        job_dir = JOBS_DIR / job_id
        source = job_dir / "source"
        source.mkdir(parents=True, exist_ok=True)

        # 1. Download the actual video file (needed for Demucs and muxing).
        def _download_progress(percent: float) -> None:
            # Map the download 0-100% onto the 2..10 progress band so the bar
            # visibly moves without colliding with later stage values.
            db.update_job(
                job_id,
                progress=round(2 + percent / 100 * 8, 1),
                wait_reason=f"กำลังดาวน์โหลดวิดีโอ {percent:.0f}%",
            )

        video_path, video_title = download_video(url, source, progress=_download_progress)
        info = probe_media(video_path)
        if not info.has_video:
            raise MediaError("วิดีโอที่ดาวน์โหลดไม่มี video stream")
        if not info.has_audio:
            raise MediaError("วิดีโอที่ดาวน์โหลดไม่มี audio stream")
        # Name the job after the clip instead of a generic id.
        job_filename = video_filename(
            video_title, video_path.suffix.lstrip(".") or "mkv", f"youtube-{job_id[:8]}"
        )

        # 2. Pull the available subtitle from YouTube, preferring the original
        #    language (the job's source_language, or the video's detected one).
        srt_text, language = fetch_subtitle(url, job.get("source_language") or "auto")
        try:
            parsed = parse_srt(srt_text.encode("utf-8"), lenient=True)
        except SrtValidationError as exc:
            raise YouTubeError(f"อ่านคำบรรยายจาก YouTube ไม่สำเร็จ: {exc}") from exc
        cues = [
            {
                "source_index": cue.source_index,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "text": cue.text,
                "warnings": list(cue.warnings),
            }
            for cue in parsed.cues
        ]
        if not cues:
            raise YouTubeError("คำบรรยายที่ดึงได้มี cue ว่าง")

        thai = language and language.split("-")[0].lower() == "th"
        db.replace_source_cues(job_id, cues)
        if thai:
            # Thai subtitle doubles as both the transcript and the dub text.
            db.replace_translation_cues(job_id, cues)
            db.update_job(
                job_id,
                status="queued",
                stage="downloaded",
                progress=5,
                mode="import",
                source_path=data_relative(video_path),
                filename=job_filename,
                video_duration_ms=round(info.duration * 1000),
                video_codec=info.video_codec,
                wait_reason=None,
                error=None,
            )
        else:
            # Non-Thai subtitle becomes the source; Gemini translates to Thai.
            db.update_job(
                job_id,
                status="queued",
                stage="downloaded",
                progress=5,
                mode="import_pending",
                source_path=data_relative(video_path),
                filename=job_filename,
                video_duration_ms=round(info.duration * 1000),
                video_codec=info.video_codec,
                wait_reason=None,
                error=None,
                source_language=language or "auto",
            )

    def _extract(self, job: dict) -> None:
        job_id = job["id"]
        db.update_job(job_id, status="extracting", progress=2, error=None, wait_reason=None)
        source = resolve_data_path(job["source_path"])
        info = probe_media(source)
        if not info.has_video:
            raise MediaError("ไฟล์ที่เลือกไม่มี video stream")
        if not info.has_audio:
            raise MediaError("วิดีโอไม่มี audio stream สำหรับถอดและแยกเสียง")
        job_dir = JOBS_DIR / job_id
        work = job_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        original = work / "original-audio.wav"
        extract_original_audio(source, original)
        db.update_job(
            job_id,
            status="queued",
            stage="extracted",
            progress=10,
            original_audio_path=data_relative(original),
            video_duration_ms=round(info.duration * 1000),
            video_codec=info.video_codec,
        )

    def _separate(self, job: dict) -> None:
        job_id = job["id"]
        db.update_job(job_id, status="separating", progress=12, wait_reason=None)
        job_dir = JOBS_DIR / job_id
        background = job_dir / "artifacts" / "background.flac"

        def should_stop() -> bool:
            current = db.get_job(job_id, include_cues=False) or {}
            return current.get("control_requested") in {"pause", "cancel"}

        def heartbeat(elapsed_seconds: float) -> None:
            minutes = int(elapsed_seconds // 60)
            # Creep the bar so a long CPU run reads as alive, not frozen.
            message = "กำลังแยกเสียงพูดด้วย Demucs"
            progress_tail = (job_dir / "work" / "demucs" / "runner.log")
            if progress_tail.is_file():
                try:
                    percent = parse_demucs_progress(
                        progress_tail.read_bytes()[-4000:].decode("utf-8", errors="replace")
                    )
                except OSError:
                    percent = None
                if percent is not None:
                    message += f" {percent:.0f}%"
            if minutes:
                message += f" ({minutes} นาทีแล้ว — บน CPU อาจใช้เวลาหลายนาที)"
            if should_stop():
                message += " — รับคำสั่งพัก/ยกเลิกแล้ว กำลังหยุด"
            db.update_job(
                job_id,
                progress=min(24, 12 + minutes // 2),
                wait_reason=message,
            )

        try:
            if (job.get("separation_mode") or "demucs") == "fast":
                # Fast mode: skip Demucs entirely, reuse the original mix as the
                # background (user controls its level via background_volume at mux).
                background.parent.mkdir(parents=True, exist_ok=True)
                original = resolve_data_path(job["original_audio_path"])
                result = subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", str(original),
                     "-map", "0:a:0", "-c:a", "flac", "-compression_level", "8", str(background)],
                    capture_output=True, text=True,
                )
                if result.returncode:
                    raise RuntimeError(f"สร้างเสียงพื้นหลังโหมดเร็วไม่สำเร็จ: {result.stderr.strip()}")
                shutil.rmtree(job_dir / "work" / "demucs", ignore_errors=True)
                db.record_attempt(job_id, "separate", "ok", model="fast-copy", message="ข้าม Demucs (โหมดเร็ว)")
            else:
                create_background_stem(
                    resolve_data_path(job["original_audio_path"]),
                    job_dir / "work",
                    background,
                    should_stop=should_stop,
                    on_heartbeat=heartbeat,
                )
        except SeparationCancelled as exc:
            # Drop partial Demucs output; the rerun starts clean on resume.
            shutil.rmtree(job_dir / "work" / "demucs", ignore_errors=True)
            if not self._handle_control(job_id):
                raise RuntimeError("การแยกเสียงถูกขัดจังหวะ") from exc
            return
        db.put_artifact(job_id, "background", background, "audio/flac")
        db.update_job(
            job_id,
            status="queued",
            stage="separated",
            progress=25,
            background_path=data_relative(background),
        )

    def _skip_to_synthesize(self, job: dict) -> None:
        """Jump straight to synthesis for imported-SRT jobs (no ASR/Gemini)."""
        job_id = job["id"]
        # Cues were already populated as the translation layer at creation.
        db.update_job(
            job_id,
            status="queued",
            stage="translated",
            progress=55,
            translation_approved=1,
            wait_reason=None,
            error=None,
        )

    def _import_pending_to_translate(self, job: dict) -> None:
        """Move non-Thai-subtitle jobs to the Gemini translate step."""
        job_id = job["id"]
        source_srt = JOBS_DIR / job_id / "artifacts" / "source.srt"
        source_srt.parent.mkdir(parents=True, exist_ok=True)
        source_srt.write_text(serialize_srt(db.source_cues(job_id), bom=True), encoding="utf-8")
        db.put_artifact(job_id, "source_srt", source_srt, "application/x-subrip")
        pause = bool(job.get("pause_after_transcription"))
        db.update_job(
            job_id,
            status="reviewing_transcript" if pause else "queued",
            stage="transcribed",
            progress=45,
            source_srt_path=data_relative(source_srt),
            transcript_approved=0 if pause else 1,
            wait_reason=None,
            error=None,
        )

    def _translate(self, job: dict) -> None:
        job_id = job["id"]
        db.update_job(job_id, status="translating", progress=47, error=None, wait_reason=None)
        job_dir = JOBS_DIR / job_id
        source = db.source_cues(job_id)
        if not source:
            raise RuntimeError("ไม่มี source cue สําหรับแปล")
        try:
            translated = translate(
                job_id, source, job_dir / "work", job.get("source_language") or "auto",
                prompt=job.get("translation_prompt"),
            )
        except QuotaWait:
            raise
        except TranslationError as exc:
            # All models answered unusably (or never returned).  Let the user
            # decide how to proceed instead of failing the whole job: park it in
            # needs_review with the reason visible in the UI.
            logger.warning("Translation exhausted all models: %s", exc)
            db.update_job(
                job_id,
                status="needs_review",
                error=None,
                wait_reason=f"แปลไม่ผ่านโมเดลทั้งหมด: {exc}",
            )
            return
        db.replace_translation_cues(job_id, translated)
        translated_srt = job_dir / "artifacts" / "translated.th.srt"
        translated_srt.parent.mkdir(parents=True, exist_ok=True)
        translated_srt.write_text(serialize_srt(translated, bom=True), encoding="utf-8")
        db.put_artifact(job_id, "translated_srt", translated_srt, "application/x-subrip")
        pause = bool(job.get("pause_after_translation"))
        db.update_job(
            job_id,
            status="reviewing_translation" if pause else "queued",
            stage="translated",
            progress=55,
            translated_srt_path=data_relative(translated_srt),
            translation_approved=0 if pause else 1,
        )

    def _synthesize(self, job: dict) -> None:
        job_id = job["id"]
        # Claim a batch up front so no cue is synthesized twice; each cue is
        # independent (own cache key, own files), so they run concurrently.
        batch: list[dict] = []
        for _ in range(max(1, TTS_SYNTH_WORKERS)):
            cue = db.next_cue(job_id)
            if cue is None:
                break
            db.update_cue(cue["id"], status="processing", error=None)
            batch.append(cue)
        if not batch:
            self._assemble_dub(job)
            return
        db.update_job(
            job_id,
            status="synthesizing",
            stage="synthesizing",
            current_cue_id=batch[0]["id"],
            wait_reason=None,
        )
        first_error: BaseException | None = None
        with ThreadPoolExecutor(
            max_workers=len(batch), thread_name_prefix="tts"
        ) as pool:
            futures = [pool.submit(self._generate_cue, job, cue) for cue in batch]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        counts = db.cue_counts(job_id)
        total = sum(counts.values()) or 1
        progress = 55 + (counts.get("completed", 0) / total) * 35
        if not self._handle_control(job_id):
            db.update_job(
                job_id,
                status="queued",
                stage="synthesizing",
                current_cue_id=None,
                progress=round(progress, 1),
            )

    def _generate_cue(self, job: dict, cue: dict) -> None:
        """Synthesize one cue on the Edge TTS voice at its natural rate.

        The cue is stored at its intrinsic duration (``speed_factor=1.0``);
        timing is handled later in ``audio.assemble``, which speeds each whole
        time segment rather than fiddling with individual cues.
        """
        job_id = job["id"]
        inference_text = str(cue["text"])
        voice = job.get("voice") or EDGE_TTS_DEFAULT_VOICE
        base_rate = int(job.get("tts_rate") or 0)
        revision = int(cue.get("generation_revision") or 0)
        job_dir = JOBS_DIR / job_id
        raw_path = job_dir / "cues" / f"{cue['position']:05d}-raw.wav"
        final_path = job_dir / "cues" / f"{cue['position']:05d}.wav"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = int(cue["attempts"]) + 1
        started = time.monotonic()

        db.update_cue(
            cue["id"],
            status="processing",
            attempts=attempts,
            inference_text=inference_text,
            error=None,
        )
        try:
            cache_key = hashlib.sha256(
                "\0".join(
                    (CACHE_FORMAT_REVISION, voice, str(base_rate), inference_text, str(revision))
                ).encode()
            ).hexdigest()
            cached = db.cache_get(cache_key)
            if cached:
                shutil.copy2(cached["path"], raw_path)
                original_ms = int(cached["duration_ms"])
            else:
                original_ms = synth_cue(inference_text, voice, base_rate, raw_path)
                cache_path = CACHE_DIR / f"{cache_key}.wav"
                shutil.copy2(raw_path, cache_path)
                db.cache_put(cache_key, str(cache_path), original_ms, {})
            db.update_cue(cue["id"], cache_key=cache_key)
            shutil.copy2(raw_path, final_path)
            # Edge TTS pads every file with leading/trailing silence (~0.2s +
            # ~0.9s); trim the placed copy so gaps and durations stay truthful.
            # The cache keeps the untrimmed master; trimming is deterministic.
            final_ms = trim_edge_silence(final_path)
            db.update_cue(
                cue["id"],
                status="completed",
                audio_path=data_relative(final_path),
                original_duration_ms=original_ms,
                final_duration_ms=final_ms,
                speed_factor=1.0,
                warnings_json=json.dumps(cue.get("warnings") or [], ensure_ascii=False),
                generation_duration_ms=round((time.monotonic() - started) * 1000),
                pipeline_revision=PIPELINE_REVISION,
                error=None,
            )
            raw_path.unlink(missing_ok=True)
        except Exception as exc:
            if attempts < 3:
                db.update_cue(cue["id"], status="pending", error=str(exc)[-1200:])
                raise QuotaWait(
                    f"Edge TTS ขัดข้องชั่วคราว: {exc}",
                    datetime.now(UTC) + timedelta(seconds=10 * attempts),
                ) from exc
            db.update_cue(cue["id"], status="failed", error=str(exc)[-1200:])
            raise

    def _assemble_dub(self, job: dict) -> None:
        job_id = job["id"]
        # Older cue files still carry Edge TTS edge padding; trim them here so
        # a plain reassemble also fixes the dead air (and the DB durations).
        for cue in db.completed_cues(job_id):
            try:
                audio = resolve_data_path(cue["audio_path"])
            except (ValueError, KeyError, TypeError):
                continue
            if not audio.is_file():
                continue
            trimmed_ms = trim_edge_silence(audio)
            if trimmed_ms != int(cue.get("final_duration_ms") or 0):
                db.update_cue(cue["id"], final_duration_ms=trimmed_ms)
        refreshed = db.get_job(job_id)
        if not refreshed or refreshed["completed_cues"] != refreshed["total_cues"]:
            raise RuntimeError("ยังมี cue ที่สร้างเสียงไม่สำเร็จ")
        job_dir = JOBS_DIR / job_id
        artifacts = job_dir / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        wav_path, mp3_path, duration, timeline = assemble(
            job_dir, refreshed["cues"], int(refreshed.get("max_start_delay_ms", 1000)), artifacts
        )
        write_report(artifacts, refreshed, duration, timeline)
        dub_wav = artifacts / "dub.wav"
        dub_mp3 = artifacts / "dub.mp3"
        wav_path.replace(dub_wav)
        mp3_path.replace(dub_mp3)
        db.put_artifact(job_id, "dub_wav", dub_wav, "audio/wav")
        db.put_artifact(job_id, "dub_mp3", dub_mp3, "audio/mpeg")
        db.put_artifact(job_id, "report_json", artifacts / "report.json", "application/json")
        db.put_artifact(job_id, "report_csv", artifacts / "report.csv", "text/csv")
        latest_end = max(item["actual_end_ms"] for item in timeline)
        base_warnings = [
            warning
            for warning in refreshed["warnings"]
            if not warning.startswith("เสียงพากย์ยาวเกินวิดีโอ")
            and not warning.startswith("กลุ่ม cue")
        ]
        capped_groups: dict[int, list[int]] = {}
        capped_speed = 0.0
        for item in timeline:
            if item.get("group_capped"):
                capped_groups.setdefault(int(item.get("segment_index") or 0), []).append(
                    int(item["cue"]["position"])
                )
                capped_speed = max(capped_speed, float(item.get("segment_speed") or 0))
        capped_warnings = [
            f"กลุ่ม cue {min(positions)}–{max(positions)} ยาวเกินช่วงแม้เร่งทั้งก้อนสูงสุดแล้ว "
            f"({capped_speed:.2f}x) เสียงอาจล้นไปทับช่วงถัดไป"
            for positions in capped_groups.values()
        ]
        if latest_end > int(refreshed["video_duration_ms"]) + 20:
            overflow = latest_end - int(refreshed["video_duration_ms"])
            problem_positions = [
                str(item["cue"]["position"])
                for item in timeline
                if item["actual_end_ms"] > int(refreshed["video_duration_ms"])
            ]
            warnings = [
                *base_warnings,
                *capped_warnings,
                f"เสียงพากย์ยาวเกินวิดีโอ {overflow} ms; ตรวจ cue {', '.join(problem_positions)}",
            ]
            db.update_job(
                job_id,
                status="needs_review",
                stage="synthesizing",
                progress=90,
                dub_audio_path=data_relative(dub_wav),
                warnings_json=json.dumps(warnings, ensure_ascii=False),
                wait_reason="แก้ cue ช่วงท้ายก่อนประกอบวิดีโอ",
                error=None,
            )
            return
        db.update_job(
            job_id,
            status="queued",
            stage="synthesized",
            progress=92,
            dub_audio_path=data_relative(dub_wav),
            warnings_json=json.dumps([*base_warnings, *capped_warnings], ensure_ascii=False),
        )

    def _mux(self, job: dict) -> None:
        job_id = job["id"]
        db.update_job(job_id, status="muxing", progress=94, error=None)
        job_dir = JOBS_DIR / job_id
        output = job_dir / "artifacts" / "final.th-dub.mp4"
        copied = mix_output(
            resolve_data_path(job["source_path"]),
            resolve_data_path(job["background_path"]),
            resolve_data_path(job["dub_audio_path"]),
            output,
            float(job["video_duration_ms"]) / 1000,
            float(job["background_volume"]),
            float(job["voice_volume"]),
        )
        info = probe_media(output)
        if abs(info.duration * 1000 - int(job["video_duration_ms"])) > 150:
            output.unlink(missing_ok=True)
            raise MediaError("ความยาววิดีโอผลลัพธ์ต่างจากต้นฉบับเกิน 0.15 วินาที")

        # Also copy the finished video to the user-chosen export folder, or to
        # the shared outputs folder (named after the clip) when none was chosen.
        exported_path: Path | None = None
        export_dir = job.get("output_dir")
        if export_dir:
            local_dir = Path(export_dir)
            if not local_dir.is_dir():
                logger.warning("โฟลเดอร์ส่งออกไม่มีอยู่จริง: %s", local_dir)
            else:
                exported_path = unique_export_path(
                    local_dir, f"{Path(job['filename']).stem}.th-dub", ".mp4"
                )
                shutil.copy2(output, exported_path)
        else:
            OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
            exported_path = unique_export_path(
                OUTPUTS_DIR, f"{Path(job['filename']).stem}.th-dub", ".mp4"
            )
            shutil.copy2(output, exported_path)

        db.put_artifact(job_id, "final_video", output, "video/mp4")
        shutil.rmtree(job_dir / "work", ignore_errors=True)
        db.update_job(
            job_id,
            status="completed",
            stage="completed",
            progress=100,
            output_video_path=data_relative(output),
            video_stream_copied=int(copied),
            completed_at=db.utc_now(),
            current_cue_id=None,
            wait_reason=None,
        )


worker = JobWorker()
