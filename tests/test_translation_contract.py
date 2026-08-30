from __future__ import annotations

import pytest

import app.services.translation as translation
from app.services.translation import (
    Chunk,
    TranslationError,
    build_chunks,
    parse_model_srt,
    serialize_srt,
    split_chunk,
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


def test_translation_validation_handles_short_boundary_drift() -> None:
    source = [
        {"position": 1, "source_index": "1", "start_ms": 0, "end_ms": 1000, "text": "One."},
        {"position": 2, "source_index": "2", "start_ms": 1000, "end_ms": 1500, "text": "Two."},
        {"position": 3, "source_index": "3", "start_ms": 1500, "end_ms": 2500, "text": "Three."},
    ]
    # The short middle cue is absorbed at a boundary by some models.  Keep the
    # cue mapped (and surface a warning) instead of retrying an otherwise good
    # chunk forever.
    output = """1
00:00:00,000 --> 00:00:01,000
หนึ่ง

2
00:00:01,500 --> 00:00:02,500
สาม
"""
    cues, warnings = validate_chunk(output, source, Chunk("c1", 0, 0, 2, 0, 2))
    assert 2 in cues[0]["source_cue_indexes"]
    assert any("tolerance" in warning for warning in warnings)


def test_translation_validation_does_not_hide_omitted_long_cue() -> None:
    source = [
        {"position": 1, "source_index": "1", "start_ms": 0, "end_ms": 1000, "text": "One."},
        {"position": 2, "source_index": "2", "start_ms": 1000, "end_ms": 5000, "text": "Long."},
        {"position": 3, "source_index": "3", "start_ms": 5000, "end_ms": 6000, "text": "Three."},
    ]
    output = """1
00:00:00,000 --> 00:00:01,000
หนึ่ง

2
00:00:05,000 --> 00:00:06,000
สาม
"""
    with pytest.raises(TranslationError, match="cue ต้นฉบับ 2 ไม่ถูกครอบคลุม"):
        validate_chunk(output, source, Chunk("c1", 0, 0, 2, 0, 2))


def test_split_chunk_preserves_source_coverage_and_context() -> None:
    source = [
        {
            "position": index + 1,
            "source_index": str(index + 1),
            "start_ms": index * 1000,
            "end_ms": (index + 1) * 1000,
            "text": "ประโยคจบ." if index == 3 else "คำต่อ",
        }
        for index in range(8)
    ]
    parent = Chunk("job-chunk-0001", 0, 0, 7, 0, 7)
    children = split_chunk(parent, source)
    assert children is not None
    left, right = children
    assert left.target_start == 0
    assert right.target_end == 7
    assert left.target_end + 1 == right.target_start
    assert left.context_end >= left.target_end
    assert right.context_start < right.target_start


def test_translation_plan_reuses_persisted_split_after_restart(monkeypatch) -> None:
    source = [
        {"position": index + 1, "source_index": str(index + 1), "start_ms": index * 1000,
         "end_ms": (index + 1) * 1000, "text": "ประโยค"}
        for index in range(4)
    ]
    rows = [
        {"id": "chunk-a", "chunk_index": 0, "target_start": 0, "target_end": 1,
         "context_start": 0, "context_end": 2},
        {"id": "chunk-b", "chunk_index": 1, "target_start": 2, "target_end": 3,
         "context_start": 1, "context_end": 3},
    ]
    monkeypatch.setattr(translation.db, "translation_chunks", lambda job_id: rows)
    plan = translation._translation_plan("job", source)
    assert [item.id for item in plan] == ["chunk-a", "chunk-b"]
    assert [(item.target_start, item.target_end) for item in plan] == [(0, 1), (2, 3)]


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
