import math
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.core.config import SAMPLE_RATE
from app.services.audio import (
    INLINE_FILTER_LIMIT,
    _filter_complex_args,
    analyze_audio_tail,
    assemble,
    choose_safer_candidate,
    fit_before_next_start,
    has_active_audio_tail,
    normalize_reference,
    plan_timeline,
    write_pcm_wav,
)


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
    assert final_ms > 800
    assert overlap


def test_last_cue_keeps_its_natural_duration(tmp_path):
    source, output = tmp_path / "raw.wav", tmp_path / "natural.wav"
    write_pcm_wav(source, tone(2000))
    final_ms, speed, overlap = fit_before_next_start(source, output, None)
    assert 1990 <= final_ms <= 2010
    assert speed == 1.0
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


def test_normalize_reference_and_hash(tmp_path):
    source, output = tmp_path / "source.wav", tmp_path / "reference.wav"
    write_pcm_wav(source, tone(6000))
    duration, digest, warnings = normalize_reference(source, output)
    assert 5900 <= duration <= 6100
    assert len(digest) == 64
    assert warnings == []


def test_large_filter_graph_uses_script_file(tmp_path):
    filter_file = tmp_path / "mix-filter.txt"
    graph = "a" * (INLINE_FILTER_LIMIT + 1)
    assert _filter_complex_args(graph, filter_file) == ["-filter_complex_script", str(filter_file)]
    assert filter_file.read_text(encoding="utf-8") == graph


def test_detects_active_audio_at_generated_boundary(tmp_path):
    active = tmp_path / "active.wav"
    quiet = tmp_path / "quiet.wav"
    write_pcm_wav(active, tone(500))
    write_pcm_wav(quiet, tone(400) + bytes(int(SAMPLE_RATE * 0.2) * 2))
    assert has_active_audio_tail(active)
    assert not has_active_audio_tail(quiet)


def test_adaptive_tail_metrics_distinguish_cutoff_and_silence(tmp_path):
    cutoff = tmp_path / "cutoff.wav"
    natural = tmp_path / "natural.wav"
    write_pcm_wav(cutoff, tone(500))
    write_pcm_wav(natural, tone(400) + bytes(int(SAMPLE_RATE * 0.1) * 2))
    assert analyze_audio_tail(cutoff)["suspected_cutoff"] is True
    assert analyze_audio_tail(natural)["suspected_cutoff"] is False


def test_candidate_selection_prefers_cleaner_tail():
    abrupt = {"metrics": {"suspected_cutoff": True, "trailing_silence_ms": 0, "tail_db": -10}}
    clean = {"metrics": {"suspected_cutoff": False, "trailing_silence_ms": 50, "tail_db": -40}}
    assert choose_safer_candidate(abrupt, clean) is clean


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
    assert len(commands) == 23  # 21 stems + final WAV + MP3
    assert max(len(subprocess.list2cmdline(command)) for command in commands) < 20_000
