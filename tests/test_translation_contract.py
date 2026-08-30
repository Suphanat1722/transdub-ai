from __future__ import annotations

import pytest

from app.services.translation import (
    Chunk,
    TranslationError,
    build_chunks,
    parse_model_srt,
    serialize_srt,
    validate_chunk,
)


def source_cues() -> list[dict]:
    return [
        {"position": 1, "source_index": "1", "start_ms": 0, "end_ms": 1000, "text": "Hello."},
        {"position": 2, "source_index": "2", "start_ms": 1000, "end_ms": 2200, "text": "How are you?"},
    ]


def test_srt_round_trip_and_chunk_context() -> None:
    source = source_cues()
    rendered = serialize_srt(source)
    parsed = parse_model_srt(rendered)
    assert [(c["start_ms"], c["end_ms"]) for c in parsed] == [(0, 1000), (1000, 2200)]
    chunks = build_chunks("job", source)
    assert [(c.target_start, c.target_end) for c in chunks] == [(0, 1)]


def test_translation_validation_maps_source_and_repairs_overlap() -> None:
    source = source_cues()
    chunk = Chunk("c1", 0, 0, 1, 0, 1)
    output = """1
00:00:00,000 --> 00:00:01,200
สวัสดี

2
00:00:01,000 --> 00:00:02,200
สบายดีไหม
"""
    cues, warnings = validate_chunk(output, source, chunk)
    assert cues[1]["start_ms"] == 1200
    assert cues[0]["source_cue_indexes"] == [1, 2]
    assert "ปรับเวลาเริ่ม cue ที่ซ้อนกัน" in warnings


@pytest.mark.parametrize(
    "text",
    [
        "1\n00:00:00,000 --> 00:00:02,200\nสวัสดี AI\n",
        "1\n00:00:00,000 --> 00:00:02,300\nสวัสดี\n",
        "1\n00:00:00,000 --> 00:00:02,200\n<b>สวัสดี</b>\n",
    ],
)
def test_translation_validation_rejects_tts_unsafe_output(text: str) -> None:
    with pytest.raises(TranslationError):
        validate_chunk(text, source_cues(), Chunk("c1", 0, 0, 1, 0, 1))

