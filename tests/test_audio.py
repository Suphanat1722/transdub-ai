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
    build_speech_groups,
    plan_timeline,
    trim_edge_silence,
    write_pcm_wav,
)

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")


def tone(duration_ms: int, frequency: int = 440) -> bytes:
    frames = int(SAMPLE_RATE * duration_ms / 1000)
    return b"".join(
        struct.pack("<h", int(5000 * math.sin(2 * math.pi * frequency * i / SAMPLE_RATE)))
        for i in range(frames)
    )


def test_trim_edge_silence_removes_tts_padding_but_keeps_speech(tmp_path):
    silence = b"\x00\x00" * int(SAMPLE_RATE * 0.9)
    path = tmp_path / "padded.wav"
    write_pcm_wav(path, b"\x00\x00" * int(SAMPLE_RATE * 0.8) + tone(1000) + silence)
    trimmed_ms = trim_edge_silence(path)
    # 1000ms tone + 120ms kept padding each side (within window tolerance).
    assert 1180 <= trimmed_ms <= 1320
    # Idempotent: a second pass barely changes anything.
    assert trim_edge_silence(path) == trimmed_ms


def test_trim_edge_silence_leaves_tight_and_silent_files(tmp_path):
    tight = tmp_path / "tight.wav"
    write_pcm_wav(tight, tone(500))
    assert trim_edge_silence(tight) == 500
    quiet = tmp_path / "quiet.wav"
    write_pcm_wav(quiet, b"\x00\x00" * int(SAMPLE_RATE * 0.5))
    assert trim_edge_silence(quiet) == 500


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
    # 1305 subtitle-contiguous cues form one group: 21 part-stems (64 each) +
    # 1 group mix + 1 master mix + 1 limiter/pad + 1 MP3 = 25.
    assert len(commands) == 25
    assert max(len(subprocess.list2cmdline(command)) for command in commands) < 20_000


def test_build_speech_groups_splits_only_on_real_pauses():
    def item(req, end):
        return {"requested_start_ms": req, "actual_start_ms": req,
                "actual_end_ms": end, "cue": {"end_ms": end}}

    groups = build_speech_groups(
        [item(0, 2000), item(2100, 3000), item(5000, 6000)], max_gap_ms=400
    )
    assert [[i["requested_start_ms"] for i in group] for group in groups] == [
        [0, 2100],
        [5000],
    ]
    # Overlapping subtitles (negative gap) stay together.
    groups = build_speech_groups([item(0, 3000), item(2000, 5000)], max_gap_ms=400)
    assert len(groups) == 1


def test_assemble_groups_and_speeds_whole_group(tmp_path, monkeypatch):
    # Two subtitle-contiguous cues (200ms gap) form one group and share a
    # single uniform speed; a far cue stays isolated at its natural rate.
    first = tmp_path / "g0a.wav"
    write_pcm_wav(first, tone(4000))   # 4s
    second = tmp_path / "g0b.wav"
    write_pcm_wav(second, tone(2000))  # 2s
    third = tmp_path / "g1.wav"
    write_pcm_wav(third, tone(1000))   # 1s

    # Cue 1 (0..2000, 4s audio) and cue 2 (2200..5000, 2s audio) are one
    # subtitle run; back-to-back they span 6s for a 5s window: uniform 1.2x.
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(first),
         "start_ms": 0, "end_ms": 2000, "final_duration_ms": 4000},
        {"position": 2, "status": "completed", "audio_path": str(second),
         "start_ms": 2200, "end_ms": 5000, "final_duration_ms": 2000},
        {"position": 3, "status": "completed", "audio_path": str(third),
         "start_ms": 600000, "end_ms": 601000, "final_duration_ms": 1000},
    ]
    wav, mp3, duration, timeline = assemble(tmp_path, cues, max_start_delay_ms=1000)
    assert wav.is_file() and mp3.is_file()

    assert [item["segment_index"] for item in timeline] == [0, 0, 1]
    group0 = [item for item in timeline if item["segment_index"] == 0]
    group1 = [item for item in timeline if item["segment_index"] == 1]
    # One uniform speed across the whole group for a long and a short cue.
    assert group0[0]["segment_speed"] == group0[1]["segment_speed"] == 1.2
    assert all(item["segment_speed"] == 1.0 for item in group1)
    # Back-to-back inside the group: no remaining overlap.
    assert group0[1]["actual_start_ms"] >= group0[0]["actual_end_ms"]
    assert all(item["overlap_ms"] == 0 for item in timeline)
    # Master reaches cue 3.
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


def test_assemble_slows_short_group_to_fill_window(tmp_path):
    # Cue 1 (3s audio, 0..2000 slot) is sped to fit; cue 2 (1s audio in a
    # 2s slot across a real 500ms pause) forms its own group and is slowed
    # with one uniform speed instead of leaving dead air.
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
    # Cue 2 keeps its requested start (previous fitted audio already ended).
    assert timeline[1]["actual_start_ms"] == 2500
    assert timeline[1]["delay_ms"] == 0
    assert timeline[1]["segment_speed"] == 0.8
    assert 4490 <= duration <= 4510


def test_assemble_group_slowdown_is_floored(tmp_path):
    # A tiny line in a big window slows only to the floor, leaving honest
    # silence instead of drunken speech.
    audio = tmp_path / "short.wav"
    write_pcm_wav(audio, tone(500))
    cues = [
        {"position": 1, "status": "completed", "audio_path": str(audio),
         "start_ms": 0, "end_ms": 5000, "final_duration_ms": 500},
    ]
    _, _, duration, timeline = assemble(tmp_path, cues)
    assert timeline[0]["segment_speed"] == 0.8
    assert timeline[0]["actual_start_ms"] == 0
    assert 4990 <= duration <= 5010