from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import wave
from array import array
from pathlib import Path

from ..core.config import (
    ASSEMBLY_STEM_SIZE,
    CHANNELS,
    MAX_SPEED,
    MAX_SUBPROCESS_COMMAND_CHARS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    ffmpeg_path,
    resolve_data_path,
)


class AudioError(RuntimeError):
    pass


INLINE_FILTER_LIMIT = 4000


def _filter_complex_args(filter_graph: str, filter_file: Path) -> list[str]:
    """Return portable FFmpeg filter arguments without exceeding Windows command limits."""
    if len(filter_graph) <= INLINE_FILTER_LIMIT:
        return ["-filter_complex", filter_graph]
    filter_file.write_text(filter_graph, encoding="utf-8")
    return ["-filter_complex_script", str(filter_file)]


def normalize_reference(source: Path, target: Path) -> tuple[int, str, list[str]]:
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Decode only in FFmpeg.  The previous reverse/silenceremove/loudnorm
    # chain can remain alive indefinitely with some FFmpeg 6/7 builds.  Edge
    # trimming and loudness normalization are deterministic and safer on the
    # PCM samples below, while preserving pauses inside the reference.
    result = subprocess.run(
        [
            binary,
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-sample_fmt",
            "s16",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise AudioError(result.stderr.strip() or "แปลงไฟล์เสียงอ้างอิงไม่สำเร็จ")
    with wave.open(str(target), "rb") as source_wav:
        if source_wav.getsampwidth() != 2 or source_wav.getnchannels() != 1:
            raise AudioError("FFmpeg แปลงเสียงอ้างอิงเป็น PCM mono 16-bit ไม่สำเร็จ")
        rate = source_wav.getframerate()
        samples = array("h")
        samples.frombytes(source_wav.readframes(source_wav.getnframes()))
    if not samples:
        target.unlink(missing_ok=True)
        raise AudioError("ไฟล์เสียงอ้างอิงไม่มีข้อมูลเสียง")

    threshold = int(32768 * (10 ** (-45 / 20)))
    required_active = max(1, round(rate * 0.02))

    # Measure activity in short windows instead of requiring every sample to
    # be above the threshold (a voiced waveform naturally crosses zero).
    active_blocks = []
    for offset in range(0, len(samples), required_active):
        block = samples[offset : offset + required_active]
        rms = math.sqrt(sum(sample * sample for sample in block) / max(len(block), 1))
        if rms > threshold:
            active_blocks.append(offset)
    if not active_blocks:
        target.unlink(missing_ok=True)
        raise AudioError("ไฟล์เสียงอ้างอิงเงียบทั้งหมด")
    first_active = active_blocks[0]
    last_active = min(len(samples), active_blocks[-1] + required_active)
    trimmed = samples[first_active:last_active]

    rms = math.sqrt(sum(sample * sample for sample in trimmed) / len(trimmed))
    peak = max(abs(sample) for sample in trimmed)
    gain = (32768 * (10 ** (-20 / 20))) / max(rms, 1.0)
    peak_limit = (32768 * (10 ** (-2 / 20))) / max(peak, 1)
    gain = min(gain, peak_limit)
    normalized = array("h", (max(-32768, min(32767, round(sample * gain))) for sample in trimmed))
    write_pcm_wav(target, normalized.tobytes())

    duration = wav_duration_ms(target)
    if duration < 3000 or duration > 30000:
        target.unlink(missing_ok=True)
        raise AudioError("เสียงอ้างอิงหลังตัด silence ต้องยาวระหว่าง 3–30 วินาที")
    warnings = []
    if duration < 5000 or duration > 15000:
        warnings.append("แนะนำให้ใช้เสียงอ้างอิงที่ยาว 5–15 วินาทีเพื่อผลลัพธ์ที่ดี")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return duration, digest, warnings


def write_pcm_wav(path: Path, pcm: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return wav_duration_ms(path)


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as source:
        return round(source.getnframes() * 1000 / source.getframerate())


def has_active_audio_tail(path: Path, tail_ms: int = 120, threshold_db: float = -35.0) -> bool:
    """Detect speech-like energy reaching the generated boundary, which suggests truncation."""
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise AudioError("ตรวจปลายเสียงได้เฉพาะ WAV PCM 16-bit")
        sample_rate = source.getframerate()
        channels = source.getnchannels()
        tail_frames = min(source.getnframes(), max(1, round(sample_rate * tail_ms / 1000)))
        source.setpos(source.getnframes() - tail_frames)
        samples = array("h")
        samples.frombytes(source.readframes(tail_frames))
    if channels > 1:
        samples = array("h", samples[::channels])
    if not samples:
        return False
    normalized_rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
    tail_rms_db = 20 * math.log10(max(normalized_rms, 1e-12))
    return tail_rms_db > threshold_db


def analyze_audio_tail(path: Path) -> dict[str, float | int | bool]:
    """Measure the final 80 ms using the conservative two-pass cutoff policy."""
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise AudioError("ตรวจปลายเสียงได้เฉพาะ WAV PCM 16-bit")
        rate = source.getframerate()
        channels = source.getnchannels()
        frames = min(source.getnframes(), max(1, round(rate * 0.16)))
        source.setpos(source.getnframes() - frames)
        values = array("h")
        values.frombytes(source.readframes(frames))
    mono = list(values[::channels]) if channels > 1 else list(values)
    window = max(1, round(rate * 0.08))

    def level(samples: list[int]) -> float:
        if not samples:
            return -120.0
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
        return 20 * math.log10(max(rms, 1e-6))

    tail = mono[-window:]
    previous = mono[-2 * window : -window]
    tail_db = level(tail)
    previous_db = level(previous)
    silence_samples = 0
    threshold = 32768 * (10 ** (-40 / 20))
    for sample in reversed(mono):
        if abs(sample) > threshold:
            break
        silence_samples += 1
    trailing_silence_ms = round(silence_samples * 1000 / rate)
    decay_db = tail_db - previous_db
    suspected = tail_db > -31 and trailing_silence_ms < 40 and decay_db > -6
    return {
        "tail_db": round(tail_db, 2),
        "previous_db": round(previous_db, 2),
        "decay_db": round(decay_db, 2),
        "trailing_silence_ms": trailing_silence_ms,
        "suspected_cutoff": suspected,
    }


def choose_safer_candidate(first: dict, second: dict) -> dict:
    """Prefer a clean ending, then more silence and a quieter final window."""
    return min(
        (first, second),
        key=lambda item: (
            bool(item["metrics"]["suspected_cutoff"]),
            -int(item["metrics"]["trailing_silence_ms"]),
            float(item["metrics"]["tail_db"]),
        ),
    )


def fit_before_next_start(
    source: Path,
    target: Path,
    available_ms: int | None,
    max_speed: float = MAX_SPEED,
) -> tuple[int, float, bool]:
    """Speed up a cue to fit before the next start, trimming the tail as a last resort.

    Unlike the old behaviour (which left a reach-over cue overlapping the next
    one), if the fastest allowed speed still overruns ``available_ms`` we trim
    the tail so the cue ends on time.  Returns ``(final_ms, speed, reached_next)``
    where ``reached_next`` is False once the cue has been fitted.
    """
    original_ms = wav_duration_ms(source)
    if available_ms is None or original_ms <= available_ms:
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return original_ms, 1.0, False
    required = original_ms / available_ms if available_ms > 0 else float("inf")
    speed = min(required, max_speed)
    final_ms = round(original_ms / speed)
    # Trim the tail if the fastest allowed speed still overruns the slot.
    if final_ms > available_ms:
        trimmed = True
        final_ms = available_ms
    else:
        trimmed = False
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    args = [
        binary,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-filter:a",
        f"atempo={speed:.8f}",
    ]
    if trimmed:
        args.extend(["-t", f"{final_ms / 1000:.3f}"])
    args.extend(
        [
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            str(CHANNELS),
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
    )
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise AudioError(result.stderr.strip() or "FFmpeg ปรับความเร็วไม่สําเร็จ")
    measured = wav_duration_ms(target)
    return measured, speed, measured > available_ms + 20


def plan_timeline(cues: list[dict], max_start_delay_ms: int) -> list[dict]:
    """Delay each cue only enough to avoid prior speech, capped relative to its SRT start."""
    timeline = []
    latest_end_ms = 0
    for cue in sorted(cues, key=lambda item: item["position"]):
        if cue["status"] != "completed" or not cue.get("audio_path"):
            continue
        requested_start_ms = int(cue["start_ms"])
        audio_ms = int(cue.get("final_duration_ms") or wav_duration_ms(Path(cue["audio_path"])))
        required_delay_ms = max(0, latest_end_ms - requested_start_ms)
        applied_delay_ms = min(required_delay_ms, max_start_delay_ms)
        actual_start_ms = requested_start_ms + applied_delay_ms
        overlap_ms = max(0, latest_end_ms - actual_start_ms)
        actual_end_ms = actual_start_ms + audio_ms
        latest_end_ms = max(latest_end_ms, actual_end_ms)
        timeline.append(
            {
                "cue": cue,
                "requested_start_ms": requested_start_ms,
                "actual_start_ms": actual_start_ms,
                "actual_end_ms": actual_end_ms,
                "audio_ms": audio_ms,
                "delay_ms": applied_delay_ms,
                "overlap_ms": overlap_ms,
            }
        )
    return timeline


def assemble(
    job_dir: Path, cues: list[dict], max_start_delay_ms: int = 1000, output_dir: Path | None = None
) -> tuple[Path, Path, int, list[dict]]:
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    timeline = plan_timeline(cues, max_start_delay_ms)
    if not timeline:
        raise AudioError("ไม่มีเสียงที่พร้อมประกอบ")

    output_dir = output_dir or job_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stem_dir = output_dir / ".stems"
    stem_dir.mkdir(exist_ok=True)

    def cue_audio_path(value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not resolved.is_relative_to(job_dir.resolve()):
                raise AudioError("เส้นทางเสียง cue อยู่นอกโปรเจกต์")
            return resolved
        return resolve_data_path(candidate)

    def run(args: list[str], message: str) -> None:
        command_chars = len(subprocess.list2cmdline(args))
        if command_chars >= MAX_SUBPROCESS_COMMAND_CHARS:
            raise AudioError(f"คำสั่ง FFmpeg ยาวเกินขีดจำกัดภายใน ({command_chars} ตัวอักษร)")
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode:
            raise AudioError(result.stderr.strip() or message)

    stems: list[tuple[Path, int]] = []
    for stem_index, start in enumerate(range(0, len(timeline), ASSEMBLY_STEM_SIZE)):
        group = timeline[start : start + ASSEMBLY_STEM_SIZE]
        origin = min(item["actual_start_ms"] for item in group)
        args = [binary, "-y", "-v", "error"]
        filters = []
        for index, item in enumerate(group):
            args.extend(["-i", str(cue_audio_path(item["cue"]["audio_path"]))])
            filters.append(f"[{index}:a]adelay={item['actual_start_ms'] - origin}:all=1[a{index}]")
        labels = "".join(f"[a{i}]" for i in range(len(group)))
        filters.append(
            f"{labels}amix=inputs={len(group)}:duration=longest:normalize=0,aresample={SAMPLE_RATE}[out]"
        )
        stem = stem_dir / f"stem-{stem_index:03d}.wav"
        filter_args = _filter_complex_args(";\n".join(filters), stem_dir / f"filter-{stem_index:03d}.txt")
        args.extend(
            [
                *filter_args,
                "-map",
                "[out]",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_f32le",
                str(stem),
            ]
        )
        run(args, "FFmpeg สร้าง stem ไม่สำเร็จ")
        stems.append((stem, origin))

    wav_out = output_dir / "output.wav"
    mp3_out = output_dir / "output.mp3"
    args = [binary, "-y", "-v", "error"]
    filters = []
    for index, (stem, origin) in enumerate(stems):
        args.extend(["-i", str(stem)])
        filters.append(f"[{index}:a]adelay={origin}:all=1[s{index}]")
    labels = "".join(f"[s{i}]" for i in range(len(stems)))
    final_end_ms = max(
        max(item["actual_end_ms"] for item in timeline),
        max(
            int(cue.get("end_ms", int(cue["start_ms"]) + int(cue.get("final_duration_ms") or 0)))
            for cue in cues
        ),
    )
    # Give the master a finite silent input so a subtitle end time is always
    # represented, without relying on ``apad`` (which can stay open forever
    # with some FFmpeg filter graphs).  The final -t below still caps output
    # to the latest actual/subtitle end when speech runs longer.
    silence_input = len(stems)
    args.extend(
        [
            "-f",
            "lavfi",
            "-t",
            f"{final_end_ms / 1000:.3f}",
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
        ]
    )
    filters.append(
        f"{labels}[{silence_input}:a]amix=inputs={len(stems) + 1}:duration=longest:normalize=0,"
        f"alimiter=limit=0.95:attack=5:release=50,aresample={SAMPLE_RATE}[out]"
    )
    filter_args = _filter_complex_args(";\n".join(filters), stem_dir / "final-filter.txt")
    args.extend(
        [
            *filter_args,
            "-map",
            "[out]",
            "-t",
            f"{final_end_ms / 1000:.3f}",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(wav_out),
        ]
    )
    run(args, "FFmpeg ประกอบเสียงไม่สำเร็จ")
    run(
        [binary, "-y", "-v", "error", "-i", str(wav_out), "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_out)],
        "FFmpeg สร้าง MP3 ไม่สำเร็จ",
    )
    shutil.rmtree(stem_dir, ignore_errors=True)
    return wav_out, mp3_out, wav_duration_ms(wav_out), timeline


def write_report(job_dir: Path, job: dict, output_duration_ms: int, timeline: list[dict]) -> None:
    timing = [
        {
            "position": item["cue"]["position"],
            "source_index": item["cue"]["source_index"],
            "text": item["cue"]["text"],
            "requested_start_ms": item["requested_start_ms"],
            "subtitle_end_ms": item["cue"]["end_ms"],
            "actual_start_ms": item["actual_start_ms"],
            "actual_end_ms": item["actual_end_ms"],
            "audio_ms": item["audio_ms"],
            "delay_ms": item["delay_ms"],
            "overlap_ms": item["overlap_ms"],
            "speed_factor": item["cue"]["speed_factor"],
            "warnings": item["cue"].get("warnings", []),
            "inference_text": item["cue"].get("inference_text") or item["cue"]["text"],
            "effective_seed": item["cue"].get("effective_seed") or item["cue"].get("seed"),
            "generation_revision": item["cue"].get("generation_revision", 0),
            "duration_multiplier": item["cue"].get("duration_multiplier"),
            "generation_passes": item["cue"].get("generation_passes", 0),
            "generation_duration_ms": item["cue"].get("generation_duration_ms"),
            "tail_metrics": item["cue"].get("tail_metrics", {}),
            "pipeline_revision": item["cue"].get("pipeline_revision"),
        }
        for item in timeline
    ]
    report = {
        "job_id": job["id"],
        "source": job["filename"],
        "voice_profile": job.get("voice_profile_name"),
        "model": job["model"],
        "nfe_step": job.get("nfe_step"),
        "inference_speed": job.get("inference_speed"),
        "max_start_delay_ms": job.get("max_start_delay_ms"),
        "output_duration_ms": output_duration_ms,
        "timeline": timing,
        "delayed_cues": [item for item in timing if item["delay_ms"] > 0],
        "overlapping_cues": [item for item in timing if item["overlap_ms"] > 0],
        "warnings": job["warnings"],
        "pipeline_revision": job.get("pipeline_revision"),
    }
    (job_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "position,source_index,requested_start_ms,subtitle_end_ms,actual_start_ms,"
        "actual_end_ms,audio_ms,delay_ms,overlap_ms,speed_factor,generation_revision,"
        "duration_multiplier,generation_passes,warnings,inference_text,text"
    ]
    for item in timing:
        safe = item["text"].replace('"', '""')
        inference = item["inference_text"].replace('"', '""')
        warnings = " | ".join(item["warnings"]).replace('"', '""')
        lines.append(
            f"{item['position']},{item['source_index']},{item['requested_start_ms']},"
            f"{item['subtitle_end_ms']},{item['actual_start_ms']},{item['actual_end_ms']},"
            f"{item['audio_ms']},{item['delay_ms']},{item['overlap_ms']},"
            f"{item['speed_factor']:.4f},{item['generation_revision']},"
            f"{item['duration_multiplier'] or ''},{item['generation_passes']},"
            f'"{warnings}","{inference}","{safe}"'
        )
    (job_dir / "report.csv").write_text("\ufeff" + "\n".join(lines), encoding="utf-8")
