from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from .gpu import GPU_LOCK


class MediaError(RuntimeError):
    """An error that can be shown safely in the web UI."""


class SeparationCancelled(RuntimeError):
    """Raised when pause/cancel arrives while Demucs is running."""


def parse_demucs_progress(log_text: str) -> float | None:
    """Extract the latest Demucs tqdm percentage (0-100) from log text.

    Demucs writes carriage-return-separated bars like
    ``99%|████| 4375.8/4434.3 [08:40<00:08, 6.57seconds/s]``; the fraction is
    the reliable signal, so the last ``done/total`` pair wins.  Returns None
    when no progress figure is present yet (e.g. model loading).
    """
    pairs = re.findall(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", log_text.replace("\r", "\n"))
    for done, total in reversed(pairs):
        try:
            total_value = float(total)
        except ValueError:
            continue
        if total_value > 0:
            return max(0.0, min(100.0, float(done) / total_value * 100))
    return None


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None


def _run(command: Sequence[str], *, error_prefix: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MediaError(f"ไม่พบโปรแกรม {command[0]} กรุณาติดตั้งและเพิ่มไว้ใน PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        message = detail[-1] if detail else "ไม่ทราบรายละเอียด"
        raise MediaError(f"{error_prefix}: {message}") from exc


def _run_watched(
    command: Sequence[str],
    *,
    error_prefix: str,
    log_path: Path | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[float], None] | None = None,
    beat_seconds: float = 30.0,
) -> None:
    """Run a long subprocess with liveness beats and cooperative cancellation.

    Demucs on CPU takes many minutes with no output of its own surfaced, so a
    frozen progress bar is indistinguishable from a hang.  The caller gets a
    heartbeat every ``beat_seconds`` (to report elapsed time) and may stop the
    run by making ``should_stop`` true; the process is then terminated and
    ``SeparationCancelled`` is raised so the worker can apply pause/cancel
    immediately instead of waiting out the run.  stderr goes to ``log_path``
    (a pipe would fill and block); its tail is attached to failure messages.
    """
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        log_file = stack.enter_context(open(log_path, "wb")) if log_path is not None else None
        try:
            proc = subprocess.Popen(
                list(command),
                stdout=subprocess.DEVNULL,
                stderr=log_file or subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"ไม่พบโปรแกรม {command[0]} กรุณาติดตั้งและเพิ่มไว้ใน PATH") from exc
        start = time.monotonic()
        last_beat = start
        while True:
            code = proc.poll()
            if code is not None:
                break
            if should_stop is not None and should_stop():
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise SeparationCancelled("หยุดการแยกเสียงตามที่ขอ")
            now = time.monotonic()
            if on_heartbeat is not None and now - last_beat >= beat_seconds:
                last_beat = now
                on_heartbeat(now - start)
            time.sleep(0.5)
        if code != 0:
            tail = ""
            if log_path is not None and log_path.is_file():
                lines = log_path.read_bytes()[-2000:].decode("utf-8", errors="replace").strip().splitlines()
                tail = lines[-1] if lines else ""
            raise MediaError(f"{error_prefix}: {tail or f'exit {code}'}")


def probe_media(path: Path) -> MediaInfo:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration",
            "-of",
            "json",
            str(path),
        ],
        error_prefix=f"อ่านไฟล์ {path.name} ไม่สำเร็จ",
    )
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
        raw_duration = payload.get("format", {}).get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = next(
                (s.get("duration") for s in streams if s.get("duration") not in (None, "N/A")),
                None,
            )
        duration = float(raw_duration)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise MediaError(f"ไม่สามารถอ่านระยะเวลาของไฟล์ {path.name} ได้") from exc
    if duration <= 0:
        raise MediaError(f"ไฟล์ {path.name} มีระยะเวลาไม่ถูกต้อง")
    return MediaInfo(
        duration=duration,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        video_codec=video_stream.get("codec_name") if video_stream else None,
        width=video_stream.get("width") if video_stream else None,
        height=video_stream.get("height") if video_stream else None,
    )


def extract_original_audio(video_path: Path, output_path: Path) -> None:
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        error_prefix="ดึงเสียงเดิมจากวิดีโอไม่สำเร็จ",
    )


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def separate_background(
    original_audio: Path,
    separation_root: Path,
    notify: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[float], None] | None = None,
) -> tuple[Path, str]:
    device = "cuda" if _cuda_available() else "cpu"

    def invoke(selected_device: str) -> None:
        _run_watched(
            [
                sys.executable,
                "-m",
                "app.services.demucs_runner",
                "--two-stems=vocals",
                "-n",
                "htdemucs",
                "-d",
                selected_device,
                "-o",
                str(separation_root),
                str(original_audio),
            ],
            error_prefix=f"แยกเสียงด้วย Demucs ({selected_device}) ไม่สำเร็จ",
            log_path=separation_root / "runner.log",
            should_stop=should_stop,
            on_heartbeat=on_heartbeat,
        )

    with GPU_LOCK:
        try:
            invoke(device)
        except SeparationCancelled:
            raise
        except MediaError:
            if device != "cuda":
                raise
            if notify:
                notify("GPU ประมวลผลไม่สำเร็จ กำลังลองใหม่ด้วย CPU")
            shutil.rmtree(separation_root, ignore_errors=True)
            invoke("cpu")
            device = "cpu"

    background = separation_root / "htdemucs" / original_audio.stem / "no_vocals.wav"
    if not background.is_file():
        raise MediaError("Demucs ทำงานเสร็จแต่ไม่พบไฟล์เสียงพื้นหลัง")
    return background, device


def create_background_stem(
    original_audio: Path,
    work_dir: Path,
    target: Path,
    notify: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_heartbeat: Callable[[float], None] | None = None,
) -> tuple[Path, str]:
    """Run Demucs and retain only a compact lossless background artifact."""
    separation_root = work_dir / "demucs"
    background, device = separate_background(
        original_audio, separation_root, notify, should_stop, on_heartbeat
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(background),
            "-map", "0:a:0", "-c:a", "flac", "-compression_level", "8", str(target),
        ],
        error_prefix="บีบอัดเสียงพื้นหลังไม่สำเร็จ",
    )
    shutil.rmtree(separation_root, ignore_errors=True)
    return target, device


def mix_cue_preview(
    background_path: Path,
    voice_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
    background_volume: float,
    voice_volume: float,
) -> None:
    """Mix one cue's voice with the background segment under it for review.

    Used by the cue-preview endpoint so the user can hear dub and background
    together (with the job's volume settings) before committing to a full mux.
    """
    duration_text = f"{duration_seconds:.3f}"
    filters = (
        f"[0:a]aformat=sample_rates=24000:channel_layouts=mono,"
        f"volume={background_volume / 100.0:.4f}[bg];"
        f"[1:a]aformat=sample_rates=24000:channel_layouts=mono,"
        f"volume={voice_volume / 100.0:.4f}[voice];"
        f"[bg][voice]amix=inputs=2:duration=longest:normalize=0,"
        f"alimiter=limit=0.95:latency=1,atrim=0:{duration_text}[outa]"
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{start_seconds:.3f}",
            "-t",
            duration_text,
            "-i",
            str(background_path),
            "-i",
            str(voice_path),
            "-filter_complex",
            filters,
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        error_prefix="ผสมเสียงตัวอย่างไม่สำเร็จ",
    )


def _volume(value: float) -> str:
    return f"{value / 100.0:.4f}"


def mix_output(
    video_path: Path,
    background_path: Path,
    replacement_audio_path: Path,
    output_path: Path,
    duration: float,
    background_volume: float,
    voice_volume: float,
) -> bool:
    duration_text = f"{duration:.6f}"
    filters = (
        f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"volume={_volume(background_volume)},apad,atrim=0:{duration_text},asetpts=PTS-STARTPTS[bg];"
        f"[2:a]aformat=sample_rates=48000:channel_layouts=stereo,"
        f"volume={_volume(voice_volume)},apad,atrim=0:{duration_text},asetpts=PTS-STARTPTS[voice];"
        f"[bg][voice]amix=inputs=2:duration=longest:normalize=0,"
        f"alimiter=limit=0.95:latency=1,atrim=0:{duration_text}[outa]"
    )
    common = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-i",
        str(background_path),
        "-i",
        str(replacement_audio_path),
        "-filter_complex",
        filters,
        "-map",
        "0:v:0",
        "-map",
        "[outa]",
        "-map_metadata",
        "0",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-t",
        duration_text,
        "-movflags",
        "+faststart",
    ]
    copy_command = [*common, "-c:v", "copy", str(output_path)]
    try:
        _run(copy_command, error_prefix="ประกอบวิดีโอแบบคงภาพเดิมไม่สำเร็จ")
        return True
    except MediaError as copy_error:
        output_path.unlink(missing_ok=True)
        encode_command = [
            *common,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
        try:
            _run(encode_command, error_prefix="เข้ารหัสวิดีโอเป็น H.264 ไม่สำเร็จ")
            return False
        except MediaError as encode_error:
            raise MediaError(f"{encode_error} (การคัดลอกภาพเดิมล้มเหลวด้วย: {copy_error})") from encode_error
