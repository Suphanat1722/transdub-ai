"""Interruptible subprocess runs (Demucs liveness + pause/cancel). No FFmpeg needed."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from app.services.media import MediaError, SeparationCancelled, _run_watched


def test_run_watched_completes() -> None:
    beats: list[float] = []
    _run_watched(
        [sys.executable, "-c", "pass"],
        error_prefix="fail",
        on_heartbeat=beats.append,
        beat_seconds=0.05,
    )


def test_run_watched_reports_log_tail(tmp_path: Path) -> None:
    with pytest.raises(MediaError, match="boom-xyz"):
        _run_watched(
            [sys.executable, "-c", "import sys; print('boom-xyz', file=sys.stderr); sys.exit(3)"],
            error_prefix="fail",
            log_path=tmp_path / "run.log",
        )
    assert "boom-xyz" in (tmp_path / "run.log").read_text(encoding="utf-8", errors="replace")


def test_run_watched_missing_binary() -> None:
    with pytest.raises(MediaError, match="ไม่พบโปรแกรม"):
        _run_watched(["definitely-not-a-real-binary-xyz"], error_prefix="fail")


def test_run_watched_cancel_is_fast_and_beats(tmp_path: Path) -> None:
    beats: list[float] = []
    start = time.monotonic()
    stop_at = start + 1.2
    with pytest.raises(SeparationCancelled):
        _run_watched(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            error_prefix="fail",
            log_path=tmp_path / "run.log",
            should_stop=lambda: time.monotonic() >= stop_at,
            on_heartbeat=beats.append,
            beat_seconds=0.05,
        )
    assert time.monotonic() - start < 15
    assert beats
