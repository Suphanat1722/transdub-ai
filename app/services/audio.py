from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

from ..core.config import (
    ASSEMBLY_STEM_SIZE,
    CHANNELS,
    MAX_SEGMENT_SPEED,
    MAX_SPEED,
    MAX_SUBPROCESS_COMMAND_CHARS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    SEGMENT_SECONDS,
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

    Note: the pipeline now speeds whole segments rather than individual cues;
    this helper is kept as a reusable utility (and for tests).
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


def _run(args: list[str], message: str) -> None:
    command_chars = len(subprocess.list2cmdline(args))
    if command_chars >= MAX_SUBPROCESS_COMMAND_CHARS:
        raise AudioError(f"คําสั่ง FFmpeg ยาวเกินขีดจํากัดภายใน ({command_chars} ตัวอักษร)")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise AudioError(result.stderr.strip() or message)


def _mix_batch(inputs: list[tuple[Path, int]], origin: int, output: Path, filter_file: Path) -> None:
    """Mix a batch of (audio, offset_ms) into a single mono stem starting at ``origin``."""
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    args = [binary, "-y", "-v", "error"]
    filters = []
    for index, (path, offset) in enumerate(inputs):
        args.extend(["-i", str(path)])
        filters.append(f"[{index}:a]adelay={offset}:all=1[a{index}]")
    labels = "".join(f"[a{i}]" for i in range(len(inputs)))
    filters.append(
        f"{labels}amix=inputs={len(inputs)}:duration=longest:normalize=0,"
        f"aresample={SAMPLE_RATE}[out]"
    )
    filter_args = _filter_complex_args(";\n".join(filters), filter_file)
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
            str(output),
        ]
    )
    _run(args, "FFmpeg สร้าง stem ไม่สําเร็จ")


def _speedup_segment(source: Path, target: Path, target_ms: int, source_duration_ms: int) -> float:
    """Speed a whole segment so it ends at ``target_ms`` from its start (origin=0).

    ``source_duration_ms`` is provided by the caller (from the timeline) because
    the raw segment stores float audio that ``wave`` cannot read directly.
    Returns the applied speed factor (1.0 when already short enough).  Caps at
    ``MAX_SEGMENT_SPEED``; if still over, keeps the sped clip (no truncation).
    """
    if target_ms <= 0 or source_duration_ms <= target_ms:
        shutil.copy2(source, target)
        return 1.0
    speed = min(source_duration_ms / target_ms, MAX_SEGMENT_SPEED)
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    _run(
        [
            binary, "-y", "-v", "error", "-i", str(source),
            "-filter:a", f"atempo={speed:.8f}",
            "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-c:a", "pcm_f32le", str(target),
        ],
        "FFmpeg เร่งความเร็วทั้งช่วงไม่สําเร็จ",
    )
    return speed


def _segments_for(cues: list[dict], segment_seconds: int) -> list[int]:
    """Return the segment index for each completed cue, in position order.

    With ``segment_seconds == 0`` every cue belongs to a single segment.
    """
    if not cues:
        return []
    if segment_seconds <= 0:
        return [0] * len(cues)
    window_ms = segment_seconds * 1000
    return [max(0, int(cue["start_ms"]) // window_ms) for cue in cues]


def assemble(
    job_dir: Path, cues: list[dict], max_start_delay_ms: int = 1000, output_dir: Path | None = None
) -> tuple[Path, Path, int, list[dict]]:
    """Assemble all cue audio into a dub master, speeding each time segment as a whole.

    Cues are grouped into fixed time segments (``SEGMENT_SECONDS``).  Within a
    segment the cues are placed on the timeline at their SRT times (delayed only
    to avoid heavy overlap), then the segment is sped with a single ``atempo`` so
    its total ends at the segment's last subtitle end.  This keeps the speaking
    rate uniform within a segment instead of panicking each cue.  Returns
    ``(wav, mp3, duration_ms, timeline)`` where timeline carries per-cue entries
    plus the segment's ``segment_index``/``segment_speed`` for reporting.
    """
    binary = ffmpeg_path()
    if not binary:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")
    completed = [c for c in cues if c["status"] == "completed" and c.get("audio_path")]
    timeline = plan_timeline(completed, max_start_delay_ms)
    if not timeline:
        raise AudioError("ไม่มีเสียงที่พร้อมประกอบ")

    output_dir = output_dir or job_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = output_dir / ".segs"
    seg_dir.mkdir(exist_ok=True)

    def cue_audio_path(value: str) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not resolved.is_relative_to(job_dir.resolve()):
                raise AudioError("เส้นทางเสียง cue อยู่นอกโปรเจกต์")
            return resolved
        return resolve_data_path(candidate)

    seg_indexes = _segments_for(completed, SEGMENT_SECONDS)
    segments: dict[int, list[dict]] = {}
    for item, seg in zip(timeline, seg_indexes, strict=True):
        item["segment_index"] = seg
        segments.setdefault(seg, []).append(item)

    master_wav = output_dir / "output.wav"
    segment_wavs: list[tuple[Path, int]] = []
    final_speed: dict[int, float] = {}

    for seg in sorted(segments):
        group = segments[seg]
        origin = min(item["actual_start_ms"] for item in group)
        # Build this segment's stem: place each cue at ``actual_start_ms - origin``.
        # A long segment may need batching so the command stays under the limit.
        inputs: list[tuple[Path, int]] = []
        raw_seg = seg_dir / f"seg-{seg:03d}-raw.wav"
        batches = [group[i:i + ASSEMBLY_STEM_SIZE] for i in range(0, len(group), ASSEMBLY_STEM_SIZE)]
        if len(batches) == 1:
            inputs = [
                (cue_audio_path(item["cue"]["audio_path"]), item["actual_start_ms"] - origin)
                for item in group
            ]
            _mix_batch(inputs, origin, raw_seg, seg_dir / f"seg-{seg:03d}-filter.txt")
        else:
            stem_parts: list[tuple[Path, int]] = []
            for bi, batch in enumerate(batches):
                part = seg_dir / f"seg-{seg:03d}-part-{bi}.wav"
                _mix_batch(
                    [
                        (cue_audio_path(item["cue"]["audio_path"]), item["actual_start_ms"] - origin)
                        for item in batch
                    ],
                    origin, part, seg_dir / f"seg-{seg:03d}-part-{bi}-filter.txt",
                )
                stem_parts.append((part, 0))
            _mix_batch(stem_parts, origin, raw_seg, seg_dir / f"seg-{seg:03d}-final-filter.txt")

        # Speed the whole segment to fit its last subtitle end.
        fitted_seg = seg_dir / f"seg-{seg:03d}.wav"
        # Target = last subtitle end (fall back to the segment's actual end when
        # the cue has no explicit subtitle end); source duration = the raw stem's
        # actual span so atempo brings it back to the target.
        target_end_ms = max(
            int(item["cue"].get("end_ms", item["actual_end_ms"])) for item in group
        )
        segment_duration_ms = max(item["actual_end_ms"] for item in group) - origin
        speed = _speedup_segment(raw_seg, fitted_seg, target_end_ms - origin, segment_duration_ms)
        final_speed[seg] = speed
        for item in group:
            item["segment_speed"] = speed
        segment_wavs.append((fitted_seg, origin))

    # Mix all sped segments into the master, delayed by each segment's origin.
    master_filters: list[dict] = []
    for index, (path, origin) in enumerate(segment_wavs):
        master_filters.append({"input": path, "delay": origin, "label": f"s{index}"})
    segment_end_ms = max(
        int(item["actual_end_ms"]) for item in timeline
    )
    final_end_ms = max(
        segment_end_ms,
        max(int(c.get("end_ms", 0)) for c in completed),
    )
    args = [binary, "-y", "-v", "error"]
    filters = []
    for f in master_filters:
        args.extend(["-i", str(f["input"])])
        filters.append(f"[{f['label']}:0]adelay={f['delay']}:all=1[{f['label']}]")
    labels = "".join(f"[{f['label']}]" for f in master_filters)
    silence_input = len(segment_wavs)
    args.extend(
        ["-f", "lavfi", "-t", f"{final_end_ms / 1000:.3f}", "-i",
         f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
    )
    filters.append(
        f"{labels}[{silence_input}:a]amix=inputs={len(segment_wavs) + 1}:duration=longest:normalize=0,"
        f"alimiter=limit=0.95:attack=5:release=50,aresample={SAMPLE_RATE}[out]"
    )
    filter_args = _filter_complex_args(";\n".join(filters), seg_dir / "final-filter.txt")
    args.extend(
        [
            *filter_args, "-map", "[out]", "-t", f"{final_end_ms / 1000:.3f}",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(master_wav),
        ]
    )
    _run(args, "FFmpeg ประกอบเสียงไม่สําเร็จ")

    mp3_out = output_dir / "output.mp3"
    _run(
        [binary, "-y", "-v", "error", "-i", str(master_wav), "-c:a", "libmp3lame", "-b:a", "192k", str(mp3_out)],
        "FFmpeg สร้าง MP3 ไม่สําเร็จ",
    )
    shutil.rmtree(seg_dir, ignore_errors=True)
    return master_wav, mp3_out, wav_duration_ms(master_wav), timeline


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
            "segment_index": item.get("segment_index"),
            "segment_speed": item.get("segment_speed"),
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
        "actual_end_ms,audio_ms,delay_ms,overlap_ms,speed_factor,segment_index,segment_speed,"
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
            f"{item['speed_factor']:.4f},{item.get('segment_index') or ''},{item.get('segment_speed') or ''},"
            f'"{warnings}","{inference}","{safe}"'
        )
    (job_dir / "report.csv").write_text("\ufeff" + "\n".join(lines), encoding="utf-8")