from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from ..core.config import (
    ASSEMBLY_STEM_SIZE,
    CHANNELS,
    GROUP_CONTIGUITY_MS,
    GROUP_MAX_SPEED,
    GROUP_MIN_SPEED,
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


def trim_edge_silence(
    path: Path, *, threshold: float = 250.0, window_ms: int = 20, keep_ms: int = 120
) -> int:
    """Strip Edge TTS leading/trailing padding in place; return duration ms.

    Every synthesized cue carries ~0.2s of leading and ~0.9s of trailing
    near-silence from the TTS MP3.  Left in place it reads as a dead-air gap
    after every sentence and inflates durations into spurious group speedups.
    Trims to the first/last window at/above ``threshold`` RMS, keeping
    ``keep_ms`` of natural padding each side.  Files already tight or fully
    silent are left untouched.  Idempotent: re-running barely changes anything.
    """
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        frames = source.readframes(params.nframes)
    if params.nchannels not in (1, 2) or params.sampwidth != 2 or not frames:
        return wav_duration_ms(path)
    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    if params.nchannels == 2:
        samples = samples[::2]
    frame_rate = params.framerate
    window = max(1, int(frame_rate * window_ms / 1000))
    keep = int(frame_rate * keep_ms / 1000)

    def loud(index: int) -> bool:
        chunk = samples[index:index + window]
        return (sum(s * s for s in chunk) / len(chunk)) ** 0.5 >= threshold

    first = next((i for i in range(0, len(samples), window) if loud(i)), None)
    if first is None:
        return wav_duration_ms(path)
    last_end = next(
        (i for i in range(len(samples), 0, -window) if loud(max(0, i - window))),
        len(samples),
    )
    start = max(0, first - keep)
    end = min(len(samples), last_end + keep)
    if start <= 0 and end >= len(samples):
        return wav_duration_ms(path)
    trimmed = samples[start:end]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(frame_rate)
        output.writeframes(struct.pack("<" + "h" * len(trimmed), *trimmed))
    return wav_duration_ms(path)


def write_pcm_wav(path: Path, pcm: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(CHANNELS)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return wav_duration_ms(path)


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


def _mix_batch(inputs: list[tuple[Path, int]], output: Path, filter_file: Path) -> None:
    """Mix a batch of (audio, offset_ms) into a single mono stem.

    Offsets are absolute milliseconds on the master timeline; the stem starts
    at 0 and callers place it with ``adelay`` when offsets are relative.
    """
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


def _mix_many(inputs: list[tuple[Path, int]], output: Path, work_dir: Path, prefix: str) -> None:
    """Mix any number of (audio, offset_ms) inputs, batching to stay portable.

    Batches of ``ASSEMBLY_STEM_SIZE`` keep every FFmpeg command under the
    Windows command-length limit; parts already encode absolute timing so they
    are re-mixed at offset 0.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if len(inputs) <= ASSEMBLY_STEM_SIZE:
        _mix_batch(inputs, output, work_dir / f"{prefix}-filter.txt")
        return
    parts = _mix_to_parts(inputs, work_dir, f"{prefix}-part")
    _mix_batch(parts, output, work_dir / f"{prefix}-final-filter.txt")


def _fit_segment(source: Path, target: Path, speed: float) -> float:
    """Apply one uniform atempo factor to a group stem (or copy at 1.0).

    ``speed`` is already clamped to ``[GROUP_MIN_SPEED, GROUP_MAX_SPEED``];
    returns the applied speed.
    """
    if speed == 1.0:
        shutil.copy2(source, target)
        return 1.0
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
        "FFmpeg ปรับความเร็วทั้งช่วงไม่สำเร็จ",
    )
    return speed


def _mix_to_parts(
    inputs: list[tuple[Path, int]], work_dir: Path, prefix: str
) -> list[tuple[Path, int]]:
    """Mix inputs into part stems of at most ``ASSEMBLY_STEM_SIZE`` each.

    Parts already encode absolute timing, so they re-mix at offset 0.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    parts: list[tuple[Path, int]] = []
    for batch_index in range(0, len(inputs), ASSEMBLY_STEM_SIZE):
        part = work_dir / f"{prefix}-{batch_index // ASSEMBLY_STEM_SIZE}.wav"
        _mix_batch(
            inputs[batch_index:batch_index + ASSEMBLY_STEM_SIZE],
            part,
            work_dir / f"{prefix}-{batch_index // ASSEMBLY_STEM_SIZE}-filter.txt",
        )
        parts.append((part, 0))
    return parts


def build_speech_groups(
    timeline: list[dict], max_gap_ms: int = GROUP_CONTIGUITY_MS
) -> list[list[dict]]:
    """Partition placed cues into subtitle-contiguous speech groups.

    A new group starts wherever the next cue's requested start lies more than
    ``max_gap_ms`` after the previous cue's subtitle end — i.e. where the
    original has a real pause worth preserving.  Everything inside one group
    shares a single uniform fit speed (faster when over, slower when under),
    so speech pace never changes mid-run and pauses are never dragged over.
    """
    groups: list[list[dict]] = []
    for item in timeline:
        if groups:
            previous = groups[-1][-1]
            gap_ms = item["requested_start_ms"] - int(
                previous["cue"].get("end_ms", previous["actual_end_ms"])
            )
            if gap_ms <= max_gap_ms:
                groups[-1].append(item)
                continue
        groups.append([item])
    return groups


def assemble(
    job_dir: Path, cues: list[dict], max_start_delay_ms: int = 1000, output_dir: Path | None = None
) -> tuple[Path, Path, int, list[dict]]:
    """Assemble all cue audio into a dub master without overlaps or dead air.

    Cues whose subtitles run contiguously form a speech group: inside the
    group cues are placed strictly back-to-back, then the whole group mix is
    fitted with a single ``atempo`` — sped when over its window, slowed when
    under — clamped to ``[GROUP_MIN_SPEED, GROUP_MAX_SPEED]``.  Every cue in
    the group therefore shares one speaking rate instead of each cue running
    at its own speed, and real pauses between groups are preserved untouched.
    Isolated exact-fit cues keep their natural rate straight to the master.
    Returns ``(wav, mp3, duration_ms, timeline)``; per-cue ``segment_index`` /
    ``segment_speed`` describe the speech group.
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

    master_wav = output_dir / "output.wav"
    master_inputs: list[tuple[Path, int]] = []
    singleton_inputs: list[tuple[Path, int]] = []
    latest_end_ms = 0

    for group_index, group in enumerate(build_speech_groups(timeline)):
        # Anchor the group at its requested start or at the previous group's
        # fitted end, whichever is later.  Anchoring at the unsped end (or
        # keeping the capped planning delay) leaves a silence gap whenever an
        # earlier group was sped up, and the inflated delay shrinks this
        # group's target window into a spurious over-speed + warning.
        origin = max(group[0]["requested_start_ms"], latest_end_ms)
        shift = origin - group[0]["actual_start_ms"]
        for item in group:
            item["actual_start_ms"] += shift
            item["actual_end_ms"] += shift
        # Lay cues strictly back-to-back: overlaps and shortfalls alike are
        # absorbed by the group's single uniform speed below.
        for previous, current in zip(group, group[1:], strict=False):
            if current["actual_start_ms"] < previous["actual_end_ms"]:
                delta = previous["actual_end_ms"] - current["actual_start_ms"]
                current["actual_start_ms"] += delta
                current["actual_end_ms"] += delta
        span_start_ms = group[0]["actual_start_ms"]
        span_end_ms = max(item["actual_end_ms"] for item in group)
        # Target = the group's last subtitle end (fall back to the actual end
        # when a cue has no explicit subtitle end).
        target_end_ms = max(
            int(item["cue"].get("end_ms", item["actual_end_ms"])) for item in group
        )
        span_ms, target_ms = span_end_ms - span_start_ms, target_end_ms - span_start_ms
        if target_ms <= 0:
            speed, capped = 1.0, False
        else:
            required = span_ms / target_ms
            capped = required > GROUP_MAX_SPEED
            speed = min(max(required, GROUP_MIN_SPEED), GROUP_MAX_SPEED)
            if abs(speed - 1.0) < 0.01:
                speed = 1.0
        for item in group:
            item["segment_index"] = group_index
            item["delay_ms"] = item["actual_start_ms"] - item["requested_start_ms"]
            item["overlap_ms"] = 0
            item["group_capped"] = capped
            item["group_overrun_ms"] = span_ms - target_ms if capped else 0

        if len(group) == 1 and speed == 1.0:
            # Isolated exact-fit cue at natural rate: straight to the master mix.
            item = group[0]
            item["segment_speed"] = 1.0
            singleton_inputs.append(
                (cue_audio_path(item["cue"]["audio_path"]), item["actual_start_ms"])
            )
            latest_end_ms = max(latest_end_ms, span_end_ms)
            continue

        raw_group = seg_dir / f"group-{group_index:03d}-raw.wav"
        _mix_many(
            [
                (cue_audio_path(item["cue"]["audio_path"]), item["actual_start_ms"] - span_start_ms)
                for item in group
            ],
            raw_group,
            seg_dir,
            f"group-{group_index:03d}",
        )
        fitted_group = seg_dir / f"group-{group_index:03d}.wav"
        speed = _fit_segment(raw_group, fitted_group, speed)
        # Rewrite placements to the fitted (post-speedup) positions so the
        # report, the next group's anchor and the video-overrun gate all see
        # where speech really lands instead of the unsped shadow.
        for item in group:
            item["actual_start_ms"] = origin + round((item["actual_start_ms"] - origin) / speed)
            item["actual_end_ms"] = origin + round((item["actual_end_ms"] - origin) / speed)
            item["delay_ms"] = item["actual_start_ms"] - item["requested_start_ms"]
            item["overlap_ms"] = 0
            item["segment_speed"] = speed
        master_inputs.append((fitted_group, span_start_ms))
        # A capped group still overruns its target; the remainder pushes the
        # following groups later (and the video-overrun gate may park the job).
        latest_end_ms = max(
            latest_end_ms, max(item["actual_end_ms"] for item in group)
        )

    # Mix isolated cues (batched with the group stems) and fitted group stems
    # into one raw master, then limit + pad/trim it to the final length.
    raw_master = seg_dir / "master-raw.wav"
    all_inputs = singleton_inputs + master_inputs
    _mix_many(all_inputs, raw_master, seg_dir, "master")
    final_end_ms = max(
        latest_end_ms,
        max(int(c.get("end_ms", 0)) for c in completed),
    )
    _run(
        [
            binary, "-y", "-v", "error", "-i", str(raw_master),
            "-filter:a", f"alimiter=limit=0.95:attack=5:release=50,aresample={SAMPLE_RATE},apad",
            "-t", f"{final_end_ms / 1000:.3f}",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(master_wav),
        ],
        "FFmpeg ประกอบเสียงไม่สําเร็จ",
    )

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