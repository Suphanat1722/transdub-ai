from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta

from ..core.config import (
    AUTOMATIC_DURATION_MULTIPLIERS,
    CACHE_DIR,
    CACHE_FORMAT_REVISION,
    JOBS_DIR,
    MODEL_NAME,
    MODEL_REVISION,
    PIPELINE_REVISION,
    data_relative,
    resolve_data_path,
)
from ..repositories import database as db
from .audio import (
    analyze_audio_tail,
    assemble,
    choose_safer_candidate,
    fit_before_next_start,
    wav_duration_ms,
    write_report,
)
from .gpu import GPU_LOCK
from .inference import inference_service
from .media import (
    MediaError,
    create_background_stem,
    extract_original_audio,
    mix_output,
    probe_media,
)
from .speech_generation import apply_glossary
from .transcription import QuotaWait, transcribe
from .translation import TranslationError, serialize_srt, translate

logger = logging.getLogger(__name__)


def derive_effective_seed(job_seed: int, position: int, generation_revision: int) -> int:
    digest = hashlib.sha256(f"{job_seed}:{position}:{generation_revision}".encode()).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


class JobWorker:
    def __init__(self, inference=inference_service) -> None:
        self.inference = inference
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
            self._extract(job)
        elif stage == "extracted":
            self._separate(job)
        elif stage == "separated":
            self._transcribe(job)
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
        create_background_stem(
            resolve_data_path(job["original_audio_path"]), job_dir / "work", background
        )
        db.put_artifact(job_id, "background", background, "audio/flac")
        db.update_job(
            job_id,
            status="queued",
            stage="separated",
            progress=25,
            background_path=data_relative(background),
        )

    def _transcribe(self, job: dict) -> None:
        job_id = job["id"]
        db.update_job(job_id, status="transcribing", progress=28, error=None, wait_reason=None)
        job_dir = JOBS_DIR / job_id
        cues = transcribe(
            job_id,
            resolve_data_path(job["original_audio_path"]),
            job_dir / "work",
            job.get("source_language") or "auto",
        )
        db.replace_source_cues(job_id, cues)
        source_srt = job_dir / "artifacts" / "source.srt"
        source_srt.parent.mkdir(parents=True, exist_ok=True)
        source_srt.write_text(serialize_srt(cues, bom=True), encoding="utf-8")
        db.put_artifact(job_id, "source_srt", source_srt, "application/x-subrip")
        pause = bool(job.get("pause_after_transcription"))
        db.update_job(
            job_id,
            status="reviewing_transcript" if pause else "queued",
            stage="transcribed",
            progress=45,
            source_srt_path=data_relative(source_srt),
            transcript_approved=0 if pause else 1,
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
                job_id, source, job_dir / "work", job.get("source_language") or "auto"
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
        if not job.get("voice_profile_id"):
            raise RuntimeError("ยังไม่ได้เลือกโปรไฟล์เสียง")
        cue = db.next_cue(job_id)
        if cue is None:
            self._assemble_dub(job)
            return
        profile = db.get_voice_profile(job["voice_profile_id"])
        if not profile:
            raise RuntimeError("ไม่พบโปรไฟล์เสียงอ้างอิง")
        model = self.inference.status()
        if model.get("state") != "ready":
            raise QuotaWait(
                model.get("error") or "กำลังโหลด JaiTTS",
                datetime.now(UTC) + timedelta(seconds=5),
            )
        db.update_job(
            job_id,
            status="synthesizing",
            stage="synthesizing",
            current_cue_id=cue["id"],
            wait_reason=None,
        )
        self._generate_cue(job, cue, profile)
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

    def _generate_cue(self, job: dict, cue: dict, profile: dict) -> None:
        job_id = job["id"]
        inference_text = apply_glossary(cue["text"], job.get("glossary", []))
        revision = int(cue.get("generation_revision") or 0)
        seed = derive_effective_seed(int(job["seed"]), int(cue["position"]), revision)
        cache_key = hashlib.sha256(
            "\0".join(
                (
                    CACHE_FORMAT_REVISION,
                    MODEL_NAME,
                    MODEL_REVISION,
                    profile["audio_hash"],
                    profile["transcript"],
                    inference_text,
                    str(seed),
                    str(job["nfe_step"]),
                    str(job["inference_speed"]),
                )
            ).encode()
        ).hexdigest()
        job_dir = JOBS_DIR / job_id
        raw_path = job_dir / "cues" / f"{cue['position']:05d}-raw.wav"
        final_path = job_dir / "cues" / f"{cue['position']:05d}.wav"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        attempts = int(cue["attempts"]) + 1
        db.update_cue(
            cue["id"],
            status="processing",
            attempts=attempts,
            effective_seed=seed,
            inference_text=inference_text,
            cache_key=cache_key,
            error=None,
        )
        started = time.monotonic()
        try:
            cached = db.cache_get(cache_key)
            passes = 0
            selected_multiplier = AUTOMATIC_DURATION_MULTIPLIERS[0]
            quality: dict = {}
            if cached:
                shutil.copy2(cached["path"], raw_path)
                original_ms = int(cached["duration_ms"])
                quality = cached.get("quality", {})
                passes = int(quality.get("generation_passes", 0))
                selected_multiplier = float(quality.get("duration_multiplier", selected_multiplier))
            else:
                candidates: list[dict] = []
                for pass_index, multiplier in enumerate(AUTOMATIC_DURATION_MULTIPLIERS, 1):
                    candidate = raw_path.with_name(f"{raw_path.stem}-pass{pass_index}.wav")
                    with GPU_LOCK:
                        self.inference.generate(
                            text=inference_text,
                            reference_audio=str(resolve_data_path(profile["audio_path"])),
                            reference_text=profile["transcript"],
                            output_file=str(candidate),
                            nfe_step=job["nfe_step"],
                            speed=job["inference_speed"],
                            seed=seed,
                            duration_multiplier=multiplier,
                        )
                    metrics = analyze_audio_tail(candidate)
                    candidates.append({"path": candidate, "metrics": metrics, "multiplier": multiplier})
                    passes = pass_index
                    if not metrics["suspected_cutoff"]:
                        break
                selected = candidates[0] if len(candidates) == 1 else choose_safer_candidate(*candidates[:2])
                selected_multiplier = float(selected["multiplier"])
                quality = {
                    **selected["metrics"],
                    "generation_passes": passes,
                    "duration_multiplier": selected_multiplier,
                }
                shutil.copy2(selected["path"], raw_path)
                for candidate in candidates:
                    candidate["path"].unlink(missing_ok=True)
                original_ms = wav_duration_ms(raw_path)
                cache_path = CACHE_DIR / f"{cache_key}.wav"
                shutil.copy2(raw_path, cache_path)
                db.cache_put(cache_key, str(cache_path), original_ms, quality)

            refreshed = db.get_job(job_id)
            assert refreshed is not None
            next_subtitle = next(
                (item for item in refreshed["cues"] if item["position"] == cue["position"] + 1),
                None,
            )
            available_ms = (
                max(0, next_subtitle["start_ms"] - cue["start_ms"]) if next_subtitle else None
            )
            final_ms, speed_factor, reaches_next = fit_before_next_start(raw_path, final_path, available_ms)
            warnings = list(cue.get("warnings") or json.loads(cue.get("warnings_json", "[]")))
            if quality.get("suspected_cutoff"):
                warnings.append("ปลายเสียงอาจถูกตัดหลังสร้างครบสองรอบ")
            if reaches_next:
                warnings.append("เสียงยังชน cue ถัดไปหลังเร่งสูงสุด")
            db.update_cue(
                cue["id"],
                status="completed",
                audio_path=data_relative(final_path),
                original_duration_ms=original_ms,
                final_duration_ms=final_ms,
                speed_factor=speed_factor,
                warnings_json=json.dumps(warnings, ensure_ascii=False),
                duration_multiplier=selected_multiplier,
                generation_passes=passes,
                tail_metrics_json=json.dumps(quality),
                generation_duration_ms=round((time.monotonic() - started) * 1000),
                pipeline_revision=PIPELINE_REVISION,
                error=None,
            )
            raw_path.unlink(missing_ok=True)
        except Exception as exc:
            if attempts < 3 and "out of memory" not in str(exc).lower():
                db.update_cue(cue["id"], status="pending", error=str(exc)[-1200:])
                raise QuotaWait(
                    f"JaiTTS ขัดข้องชั่วคราว: {exc}",
                    datetime.now(UTC) + timedelta(seconds=10 * attempts),
                ) from exc
            db.update_cue(cue["id"], status="failed", error=str(exc)[-1200:])
            raise

    def _assemble_dub(self, job: dict) -> None:
        job_id = job["id"]
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
            warnings_json=json.dumps(base_warnings, ensure_ascii=False),
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
