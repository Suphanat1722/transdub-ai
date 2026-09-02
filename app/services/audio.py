from __future__ import annotations

import json
import shutil
import subprocess
import wave
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


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as source:
        return round(source.getnframes() * 1000 / source.getframerate())


def write_pcm_wav(path: Path, pcm: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return wav_duration_ms(path)


def fit_before_next_start(
    source: Path,
    target: Path,
    available_ms: int | None,
    max_speed: float = MAX_SPEED,
    trim_tail: bool = True,
) -> tuple[int, float, bool]:
    """Speed up a cue to fit before the next start, optionally trimming the tail.

    ``trim_tail=True`` keeps the historical behaviour: if the fastest allowed
    speed still overruns ``available_ms`` the tail is cut so the cue ends on
    time.  ``trim_tail=False`` never cuts words; it speeds the whole clip up to
    ``max_speed`` and, if it still overruns, returns the natural overrun so the
    caller can re-synthesize at a higher rate or flag the cue for review.
    Returns ``(final_ms, speed, reached_next)``.
    """
    original_ms = wav_duration_ms(source)
    if available_ms is None or original_ms <= available_ms:
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return original_ms, 1.0, False
    required = original_ms / available_ms if available_ms > 0 else float("inf")
    speed = min(required, max_speed)
    final_ms = round(original_ms / speed)
    trimmed = False
    if trim_tail and final_ms > available_ms:
        trimmed = True
        final_ms = available_ms
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
            "generation_revision": item["cue"].get("generation_revision", 0),
            "generation_duration_ms": item["cue"].get("generation_duration_ms"),
            "pipeline_revision": item["cue"].get("pipeline_revision"),
        }
        for item in timeline
    ]
    report = {
        "job_id": job["id"],
        "source": job["filename"],
        "voice": job.get("voice"),
        "model": job["model"],
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
        "warnings,inference_text,text"
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
            f'"{warnings}","{inference}","{safe}"'
        )
    (job_dir / "report.csv").write_text("\ufeff" + "\n".join(lines), encoding="utf-8")
