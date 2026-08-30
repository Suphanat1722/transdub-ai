from __future__ import annotations

import pytest

from app.services.transcription import TranscriptionError, extract_words, words_to_cues


def test_extract_words_applies_chunk_offset() -> None:
    interaction = {
        "steps": [{"content": [{"annotations": [
            {"type": "word_info", "text": "Hello", "start_offset": "0.25s", "end_offset": "0.75s"}
        ]}]}]
    }
    assert extract_words(interaction, 10.0)[0]["start"] == 10.25


def test_words_are_grouped_by_punctuation_gap_and_speaker() -> None:
    words = [
        {"text": "Hello.", "start": 0.0, "end": 0.4, "speaker": None},
        {"text": "Next", "start": 2.0, "end": 2.3, "speaker": None},
        {"text": "person", "start": 2.31, "end": 2.7, "speaker": "B"},
    ]
    cues = words_to_cues(words)
    assert [cue["text"] for cue in cues] == ["Hello.", "Next", "person"]
    assert cues[-1]["end_ms"] == 2700


def test_empty_word_timestamps_are_rejected() -> None:
    with pytest.raises(TranscriptionError):
        words_to_cues([])

