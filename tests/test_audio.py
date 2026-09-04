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
    build_overlap_groups,
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
    # 1305 isolated cues bypass group stems: 21 iso parts (64 each) + 1 master
    # mix + 1 limiter/pad + 1 MP3 = 24.
    assert len(commands) == 24
    assert max(len(subprocess.list2cmdline(command)) for command in commands) < 20_000


def test_build_overlap_groups_chains_only_contacting_cues():
    def item(start, end):
        return {"actual_start_ms": start, "actual_end_ms": end}

    groups = build_overlap_groups(
        [item(0, 3000), item(2000, 5000), item(5000, 6000), item(9000, 9500)]
    )
    assert [[(i["actual_start_ms"], i["actual_end_ms"]) for i in group] for group in groups] == [
        [(0, 3000), (2000, 5000)],
        [(5000, 6000)],
        [(9000, 9500)],
    ]


def test_assemble_groups_and_speeds_whole_group(tmp_path, monkeypatch):
    # Two cues chained by overlap form one group and share a single uniform
    # speed; a far cue stays isolated at its natural rate.
    first = tmp_path / "g0a.wav"
    write_pcm_wav(first, tone(4000))   # 4s
    second = tmp_path / "g0b.wav"
    write_pcm_wav(second, tone(3000))  # 3s
    third = tmp_path / "g1.wav"
    write_pcm_wav(third, tone(1000))   # 1s

    # Cue 1 (0..2000, 4s audio) overruns cue 2 (2500..5000) past the delay cap,
    # so both land in group 0; cue 3 is isolated in group 1.
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(first),
         "start_ms": 0, "end_ms": 2000, "final_duration_ms": 4000},
        {"position": 2, "status": "completed", "audio_path": str(second),
         "start_ms": 2500, "end_ms": 5000, "final_duration_ms": 3000},
        {"position": 3, "status": "completed", "audio_path": str(third),
         "start_ms": 600000, "end_ms": 601000, "final_duration_ms": 1000},
    ]
    wav, mp3, duration, timeline = assemble(tmp_path, cues, max_start_delay_ms=1000)
    assert wav.is_file() and mp3.is_file()

    assert [item["segment_index"] for item in timeline] == [0, 0, 1]
    group0 = [item for item in timeline if item["segment_index"] == 0]
    group1 = [item for item in timeline if item["segment_index"] == 1]
    # One uniform speed across the whole group, faster than natural.
    assert group0[0]["segment_speed"] == group0[1]["segment_speed"] > 1.0
    assert all(item["segment_speed"] == 1.0 for item in group1)
    # Back-to-back inside the group: no remaining overlap.
    assert group0[1]["actual_start_ms"] >= group0[0]["actual_end_ms"]
    assert all(item["overlap_ms"] == 0 for item in timeline)
    # Group 0 (7s back-to-back) needs 1.4x for its 5s window: capped at 1.25x
    # with both cues sharing the uniform speed; master reaches cue 3.
    assert duration >= 601000
    assert duration < 700000


def test_assemble_single_overlong_cue_caps_group_speed(tmp_path):
    audio = tmp_path / "long.wav"
    write_pcm_wav(audio, tone(4000))
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(audio),
         "start_ms": 0, "end_ms": 2000, "final_duration_ms": 4000},
    ]
    _, _, duration, timeline = assemble(tmp_path, cues)
    assert timeline[0]["segment_speed"] == 1.25
    assert timeline[0]["group_capped"] is True
    assert 3150 <= duration <= 3250


def test_assemble_anchors_next_group_at_fitted_end_not_unsped_shadow(tmp_path):
    # Cue 1 (3s audio, 0..2000 slot) is sped to fit; cue 2 was pushed late by
    # the planning delay.  It must start at its requested time (the previous
    # *fitted* audio already ended), not in the unsped shadow that leaves a
    # silence gap.
    first = tmp_path / "a.wav"
    write_pcm_wav(first, tone(3000))
    second = tmp_path / "b.wav"
    write_pcm_wav(second, tone(1000))
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(first),
         "start_ms": 0, "end_ms": 2000, "final_duration_ms": 3000},
        {"position": 2, "status": "completed", "audio_path": str(second),
         "start_ms": 2500, "end_ms": 4500, "final_duration_ms": 1000},
    ]
    _, _, duration, timeline = assemble(tmp_path, cues, max_start_delay_ms=1000)
    assert [item["segment_index"] for item in timeline] == [0, 1]
    assert timeline[1]["actual_start_ms"] == 2500
    assert timeline[1]["delay_ms"] == 0
    assert timeline[1]["segment_speed"] == 1.0
    assert 4490 <= duration <= 4510