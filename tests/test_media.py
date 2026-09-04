from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.media import MediaError, mix_output, probe_media

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")


def run_ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def test_probe_rejects_invalid_file(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.mp4"
    invalid.write_bytes(b"not media")
    with pytest.raises(MediaError, match="อ่านไฟล์"):
        probe_media(invalid)


def test_mix_preserves_video_duration_and_pads_short_voice(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    background = tmp_path / "background.wav"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "output.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=2",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    )
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=2", str(background))
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=0.5", str(voice))

    copied = mix_output(video, background, voice, output, 2.0, 100, 100)
    info = probe_media(output)

    assert copied is True
    assert info.has_video and info.has_audio
    assert info.duration == pytest.approx(2.0, abs=0.1)


def test_mix_trims_long_voice_and_accepts_zero_volume(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    background = tmp_path / "background.wav"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "output.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=24:d=1",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "1", str(video),
    )
    run_ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", str(background))
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=1000:duration=3", str(voice))

    mix_output(video, background, voice, output, 1.0, 0, 150)
    assert probe_media(output).duration == pytest.approx(1.0, abs=0.1)


def test_mix_falls_back_to_h264_for_incompatible_video_codec(tmp_path: Path) -> None:
    video = tmp_path / "lossless.mkv"
    background = tmp_path / "background.wav"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "output.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=green:s=160x90:r=24:d=0.5",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-c:v", "huffyuv", "-c:a", "pcm_s16le", "-t", "0.5", str(video),
    )
    run_ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.5", str(background))
    run_ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.5", str(voice))

    copied = mix_output(video, background, voice, output, 0.5, 100, 100)

    assert copied is False
    assert probe_media(output).video_codec == "h264"


def test_mix_output_freezes_tail_when_dub_outruns_video(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    background = tmp_path / "background.wav"
    voice = tmp_path / "voice.wav"
    output = tmp_path / "output.mp4"
    run_ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:s=160x90:r=24:d=1",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "1", str(video),
    )
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", str(background))
    run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=1.5", str(voice))

    # Dub (1.5s) outruns the video (1s): the tail survives on a frozen frame
    # instead of failing the duration check.
    mix_output(video, background, voice, output, 1.5, 100, 100)
    assert probe_media(output).duration == pytest.approx(1.5, abs=0.2)
