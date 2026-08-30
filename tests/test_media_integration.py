from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.media import extract_original_audio, mix_output, probe_media

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")


def run(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def test_extract_mix_and_duration_validation(tmp_path: Path) -> None:
    video = tmp_path / "input.mp4"
    background = tmp_path / "background.wav"
    dub = tmp_path / "dub.wav"
    original = tmp_path / "original.wav"
    output = tmp_path / "output.mp4"
    run("-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25:d=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(video))
    run("-f", "lavfi", "-i", "sine=frequency=220:duration=1.5", str(background))
    run("-f", "lavfi", "-i", "sine=frequency=660:duration=1.0", str(dub))
    info = probe_media(video)
    assert info.has_video and info.has_audio
    extract_original_audio(video, original)
    assert probe_media(original).has_audio
    copied = mix_output(video, background, dub, output, info.duration, 100, 100)
    final = probe_media(output)
    assert copied is True
    assert final.has_video and final.has_audio
    assert abs(final.duration - info.duration) <= 0.15
