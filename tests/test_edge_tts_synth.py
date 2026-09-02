from __future__ import annotations

import builtins
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import edge_tts_synth


def test_rate_kwargs_formats_signed_percentage():
    assert edge_tts_synth.rate_kwargs(0) == {}
    assert edge_tts_synth.rate_kwargs(10) == {"rate": "+10%"}
    assert edge_tts_synth.rate_kwargs(-15) == {"rate": "-15%"}


def test_missing_edge_tts_raises_unavailable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "edge_tts":
            raise ImportError("no edge_tts")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(edge_tts_synth.EdgeTTSUnavailableError):
        edge_tts_synth.list_voices()
    with pytest.raises(edge_tts_synth.EdgeTTSUnavailableError):
        edge_tts_synth.synth_cue("x", "v", 0, Path("out.wav"))


def test_synth_without_ffmpeg_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1)
    )
    with pytest.raises(Exception) as exc:
        edge_tts_synth.synth_cue("สวัสดี", "th-TH-PremwadeeNeural", 0, tmp_path / "cue.wav")
    assert "FFmpeg" in str(exc.value)


def test_synth_passes_rate_to_edge_tts_and_converts(monkeypatch, tmp_path):
    import edge_tts

    seen: dict = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kwargs):
            seen["text"] = text
            seen["voice"] = voice
            seen["rate"] = kwargs.get("rate")

        async def save(self, path):
            seen["saved_path"] = str(path)
            Path(path).write_bytes(b"mp3data")

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)

    output = tmp_path / "cue.wav"

    def fake_run(args, **kwargs):
        if args[:1] == ["ffmpeg", "-version"]:
            return SimpleNamespace(returncode=0)
        if args[-1] == str(output):
            output.write_bytes(b"RIFF wav data")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # The duration read uses soundfile; stub it to keep the test offline-fast.
    monkeypatch.setattr(edge_tts_synth, "_duration_ms", lambda _path: 1000)

    ms = edge_tts_synth.synth_cue("สวัสดีครับ", "th-TH-PremwadeeNeural", 10, output)
    assert ms == 1000
    assert seen["voice"] == "th-TH-PremwadeeNeural"
    assert seen["rate"] == "+10%"
    assert Path(seen["saved_path"]).name == "cue.mp3"