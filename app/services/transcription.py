from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google import genai

from ..core.config import TRANSCRIPTION_MODEL, ffmpeg_path, gemini_api_key
from ..repositories import database as db


class TranscriptionError(RuntimeError):
    pass


class QuotaWait(TranscriptionError):
    def __init__(self, message: str, retry_at: datetime):
        super().__init__(message)
        self.retry_at = retry_at


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _offset(value: Any) -> float | None:
    if value is None:
        return None
    match = re.match(r"^\s*([0-9.]+)s?\s*$", str(value))
    return float(match.group(1)) if match else None


def _probe_duration(path: Path) -> float:
    binary = shutil.which("ffprobe")
    if not binary:
        raise TranscriptionError("ไม่พบ FFprobe ใน PATH")
    result = subprocess.run(
        [binary, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TranscriptionError("ไม่สามารถอ่านความยาวเสียงสำหรับถอดข้อความได้") from exc
    if result.returncode or duration <= 0:
        raise TranscriptionError(result.stderr.strip() or "ความยาวเสียงไม่ถูกต้อง")
    return duration


def prepare_chunks(source: Path, directory: Path, limit_seconds: int = 25 * 60) -> list[dict]:
    binary = ffmpeg_path()
    if not binary:
        raise TranscriptionError("ไม่พบ FFmpeg ใน PATH")
    directory.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(source)
    chunks: list[dict] = []
    offset = 0.0
    for index in range(max(1, math.ceil(duration / limit_seconds))):
        target = directory / f"chunk-{index:04d}.flac"
        target_duration = min(limit_seconds, duration - offset)
        if not target.is_file():
            result = subprocess.run(
                [
                    binary, "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(source),
                    "-t", f"{target_duration:.3f}", "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "flac", str(target),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise TranscriptionError(result.stderr.strip()[-1200:] or "เตรียมเสียงไม่สำเร็จ")
        actual = _probe_duration(target)
        chunks.append({"index": index, "path": target, "offset": offset, "duration": actual})
        offset += actual
    return chunks


def extract_words(interaction: Any, chunk_offset: float) -> list[dict]:
    words: list[dict] = []
    for step in _get(interaction, "steps", []) or []:
        for content in _get(step, "content", []) or []:
            for annotation in _get(content, "annotations", []) or []:
                if _get(annotation, "type") != "word_info":
                    continue
                start = _offset(_get(annotation, "start_offset"))
                end = _offset(_get(annotation, "end_offset"))
                words.append(
                    {
                        "text": str(_get(annotation, "text", "")).strip(),
                        "speaker": _get(annotation, "speaker"),
                        "start": None if start is None else round(start + chunk_offset, 3),
                        "end": None if end is None else round(end + chunk_offset, 3),
                    }
                )
    return words


def words_to_cues(words: list[dict], max_chars: int = 72) -> list[dict]:
    usable = [word for word in words if word.get("text") and word.get("start") is not None and word.get("end") is not None]
    if not usable:
        raise TranscriptionError("Gemini ไม่ส่ง word timestamps กลับมา")
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in usable:
        candidate = " ".join([*(str(item["text"]).strip() for item in current), str(word["text"]).strip()]).strip()
        gap = float(word["start"]) - float(current[-1]["end"]) if current else 0
        speaker_changed = bool(current and word.get("speaker") != current[-1].get("speaker"))
        if current and (gap > 1.2 or speaker_changed or len(candidate) > max_chars):
            groups.append(current)
            current = []
        current.append(word)
        if str(word["text"]).rstrip().endswith((".", "?", "!", "。", "！", "？")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [
        {
            "source_index": index,
            "start_ms": round(float(group[0]["start"]) * 1000),
            "end_ms": max(round(float(group[-1]["end"]) * 1000), round(float(group[0]["start"]) * 1000) + 1),
            "text": " ".join(str(word["text"]).strip() for word in group).strip(),
            "speaker": group[0].get("speaker"),
            "warnings": [],
        }
        for index, group in enumerate(groups, 1)
    ]


def _is_quota_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    text = f"{type(exc).__name__} {exc}".lower()
    return code == 429 or "429" in text or "resource_exhausted" in text


def transcribe(job_id: str, source: Path, work_dir: Path, source_language: str = "auto") -> list[dict]:
    api_key = gemini_api_key()
    if not api_key:
        raise TranscriptionError("ไม่พบ GEMINI_API_KEY ในไฟล์ .env")
    chunk_dir = work_dir / "transcription"
    chunks = prepare_chunks(source, chunk_dir)
    all_words: list[dict] = []
    with genai.Client(api_key=api_key) as client:
        for chunk in chunks:
            result_file = chunk_dir / f"result-{chunk['index']:04d}.json"
            if result_file.is_file():
                all_words.extend(json.loads(result_file.read_text(encoding="utf-8")))
                continue
            remote = None
            try:
                remote = client.files.upload(file=str(chunk["path"]))
                config: dict[str, Any] = {
                    "mode": {"type": "verbatim", "timestamp_granularities": ["word"]}
                }
                if source_language and source_language != "auto":
                    config["language_codes"] = [source_language]
                interaction = client.interactions.create(
                    model=TRANSCRIPTION_MODEL,
                    input=[{"type": "audio", "uri": remote.uri, "mime_type": remote.mime_type}],
                    generation_config={"transcription_config": config},
                )
                words = extract_words(interaction, float(chunk["offset"]))
                if not words:
                    raise TranscriptionError("Gemini ส่ง transcript ที่ไม่มี word timestamps")
                result_file.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
                all_words.extend(words)
                db.record_api_usage(
                    job_id,
                    "transcription",
                    TRANSCRIPTION_MODEL,
                    audio_seconds=float(chunk["duration"]),
                )
                db.record_attempt(
                    job_id,
                    "transcription",
                    "success",
                    unit_id=str(chunk["index"]),
                    model=TRANSCRIPTION_MODEL,
                )
                if chunk is not chunks[-1]:
                    time.sleep(6)
            except Exception as exc:
                db.record_attempt(
                    job_id,
                    "transcription",
                    "error",
                    unit_id=str(chunk["index"]),
                    model=TRANSCRIPTION_MODEL,
                    message=str(exc)[-1200:],
                )
                if _is_quota_error(exc):
                    raise QuotaWait(
                        "Gemini จำกัดอัตราการถอดเสียง จะลองใหม่ภายหลัง",
                        datetime.now(UTC) + timedelta(minutes=1),
                    ) from exc
                if isinstance(exc, TranscriptionError):
                    raise
                raise TranscriptionError(str(exc)[-1200:]) from exc
            finally:
                if remote is not None and remote.name:
                    with suppress(Exception):
                        client.files.delete(name=remote.name)
    return words_to_cues(all_words)
