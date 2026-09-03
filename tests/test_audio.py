import math
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import SAMPLE_RATE
from app.services.audio import (
    INLINE_FILTER_LIMIT,
    _filter_complex_args,
    assemble,
    fit_before_next_start,
    plan_timeline,
    write_pcm_wav,
)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")


def tone(duration_ms: int, frequency: int = 440) -> bytes:
    frames = int(SAMPLE_RATE * duration_ms / 1000)
    return b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE)))
        for i in range(frames)
    )


def test_speeds_audio_only_enough_to_avoid_the_next_start(tmp_path):
    source, output = tmp_path / "raw.wav", tmp_path / "fit.wav"
    assert 990 <= write_pcm_wav(source, tone(1000)) <= 1010
    final_ms, speed, overlap = fit_before_next_start(source, output, 800)
    assert 1.20 < speed < 1.30
    assert 780 <= final_ms <= 820
    assert not overlap


def test_caps_collision_speed_at_1_25(tmp_path):
    source, output = tmp_path / "raw.wav", tmp_path / "fit.wav"
    write_pcm_wav(source, tone(2000))
    final_ms, speed, overlap = fit_before_next_start(source, output, 800)
    assert speed == 1.25
    # Fastest allowed speed still overruns, so the tail is trimmed to fit:
    assert 780 <= final_ms <= 820
    assert not overlap


def test_last_cue_keeps_its_natural_duration(tmp_path):
    source, output = tmp_path / "raw.wav", tmp_path / "natural.wav"
    write_pcm_wav(source, tone(2000))
    final_ms, speed, overlap = fit_before_next_start(source, output, None)
    assert 1990 <= final_ms <= 2010
    assert speed == 1.0
    assert not overlap


def test_final_cue_can_speed_to_1_5(tmp_path):
    source, output = tmp_path / "raw.wav", tmp_path / "fit.wav"
    write_pcm_wav(source, tone(3000))
    # A long final cue with a narrow slot allows a caller-provided higher cap.
    final_ms, speed, overlap = fit_before_next_start(source, output, 1800, max_speed=1.5)
    assert speed == 1.5
    assert 1700 <= final_ms <= 1900
    assert not overlap


def test_assemble_timeline_and_mp3(tmp_path):
    first, second = tmp_path / "one.wav", tmp_path / "two.wav"
    write_pcm_wav(first, tone(500))
    write_pcm_wav(second, tone(500, 660))
    cues = [
        {
            "position": 1,
            "status": "completed",
            "audio_path": str(first),
            "start_ms": 0,
            "final_duration_ms": 500,
        },
        {
            "position": 2,
            "status": "completed",
            "audio_path": str(second),
            "start_ms": 1000,
            "final_duration_ms": 500,
        },
    ]
    wav, mp3, duration, timeline = assemble(tmp_path, cues)
    assert wav.is_file() and mp3.is_file()
    assert 1450 <= duration <= 1550
    assert [item["actual_start_ms"] for item in timeline] == [0, 1000]


def test_assemble_pads_master_to_last_subtitle_end(tmp_path):
    audio = tmp_path / "short.wav"
    write_pcm_wav(audio, tone(300))
    cues = [
        {
            "position": 1,
            "status": "completed",
            "audio_path": str(audio),
            "start_ms": 0,
            "end_ms": 2000,
            "final_duration_ms": 300,
        }
    ]
    _, _, duration, _ = assemble(tmp_path, cues)
    assert 1990 <= duration <= 2010


def test_timeline_uses_gap_then_caps_delay_and_allows_remaining_overlap(tmp_path):
    audio = tmp_path / "voice.wav"
    write_pcm_wav(audio, tone(500))

    within_gap = plan_timeline(
        [
            {
                "position": 1,
                "status": "completed",
                "audio_path": str(audio),
                "start_ms": 0,
                "final_duration_ms": 4000,
            },
            {
                "position": 2,
                "status": "completed",
                "audio_path": str(audio),
                "start_ms": 5000,
                "final_duration_ms": 500,
            },
        ],
        1000,
    )
    assert within_gap[1]["actual_start_ms"] == 5000
    assert within_gap[1]["delay_ms"] == 0

    capped = plan_timeline(
        [
            {
                "position": 1,
                "status": "completed",
                "audio_path": str(audio),
                "start_ms": 0,
                "final_duration_ms": 7000,
            },
            {
                "position": 2,
                "status": "completed",
                "audio_path": str(audio),
                "start_ms": 5000,
                "final_duration_ms": 500,
            },
        ],
        1000,
    )
    assert capped[1]["actual_start_ms"] == 6000
    assert capped[1]["delay_ms"] == 1000
    assert capped[1]["overlap_ms"] == 1000


def test_large_filter_graph_uses_script_file(tmp_path):
    filter_file = tmp_path / "mix-filter.txt"
    graph = "a" * (INLINE_FILTER_LIMIT + 1)
    assert _filter_complex_args(graph, filter_file) == ["-filter_complex_script", str(filter_file)]
    assert filter_file.read_text(encoding="utf-8") == graph


def test_assemble_1305_cues_keeps_every_command_under_windows_limit(tmp_path, monkeypatch):
    audio = tmp_path / "cue.wav"
    write_pcm_wav(audio, tone(100))
    cues = [
        {
            "position": position,
            "status": "completed",
            "audio_path": str(audio),
            "start_ms": (position - 1) * 200,
            "end_ms": position * 200,
            "final_duration_ms": 100,
        }
        for position in range(1, 1306)
    ]
    commands = []

    def fake_run(args, capture_output, text):
        del capture_output, text
        commands.append(args)
        target = Path(args[-1])
        if target.suffix == ".wav":
            write_pcm_wav(target, tone(100))
        else:
            target.write_bytes(b"mp3")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("app.services.audio.subprocess.run", fake_run)
    assemble(tmp_path, cues)
    # 1305 cues fit one 10-min segment: 21 part-stems (64 each) + 1 seg-mix
    # (the segment's own final) + 1 master mix + 1 MP3 = 24.
    assert len(commands) == 24
    assert max(len(subprocess.list2cmdline(command)) for command in commands) < 20_000


def test_assemble_segments_and_speeds_whole_segment(tmp_path, monkeypatch):
    # Two cues in segment 0 (0-10min) whose sum overruns their last subtitle
    # end, and one cue in segment 1 (10-20min).  The assemble must split by
    # segment and speed segment 0 as a whole instead of per-cue.
    seg0_a = tmp_path / "s0a.wav"
    write_pcm_wav(seg0_a, tone(3000))   # 3s
    seg0_b = tmp_path / "s0b.wav"
    write_pcm_wav(seg0_b, tone(3000))   # 3s
    seg1 = tmp_path / "s1.wav"
    write_pcm_wav(seg1, tone(1000))     # 1s

    # Segment 0 lasts 0s..5000ms (subtitle end 5000), but 3s+3s audio placed
    # back-to-back overruns it; segment 1 is 600_000..600_1000, fits.
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(seg0_a),
         "start_ms": 0, "end_ms": 2000, "final_duration_ms": 3000},
        {"position": 2, "status": "completed", "audio_path": str(seg0_b),
         "start_ms": 2500, "end_ms": 5000, "final_duration_ms": 3000},
        {"position": 3, "status": "completed", "audio_path": str(seg1),
         "start_ms": 600000, "end_ms": 601000, "final_duration_ms": 1000},
    ]
    wav, mp3, duration, timeline = assemble(tmp_path, cues, max_start_delay_ms=1000)
    assert wav.is_file() and mp3.is_file()

    seg_indexes = [item["segment_index"] for item in timeline]
    assert seg_indexes == [0, 0, 1]
    # Segment 0 was over and sped; its per-item segment_speed > 1.0.  Segment 1
    # fits, so its speed is 1.0.
    seg0_speeds = [item["segment_speed"] for item in timeline if item["segment_index"] == 0]
    seg1_speeds = [item["segment_speed"] for item in timeline if item["segment_index"] == 1]
    assert all(s > 1.0 for s in seg0_speeds)
    assert all(s == 1.0 for s in seg1_speeds)
    # Master is at least as long as the last subtitle end.
    assert duration >= 601000
    assert duration < 700000