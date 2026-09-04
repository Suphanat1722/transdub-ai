from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.translation as translation
from app.repositories import database as db
from app.services.translation import (
    Chunk,
    ChunkValidationError,
    TranslationError,
    _translate_chunk,
    absurd_cue_error,
    build_chunks,
    parse_model_srt,
    serialize_srt,
    split_chunk,
    validate_chunk,
)

from .test_database_pipeline import configure_temp_data


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
    # Lenient mode: an omitted long cue is accepted but surfaced as a warning
    # so the work can be reviewed in the UI instead of failing the chunk.
    cues, warnings = validate_chunk(output, source, Chunk("c1", 0, 0, 2, 0, 2))
    assert any("cue ต้นฉบับ 2 ไม่ถูกครอบคลุม" in warning for warning in warnings)


def test_translation_validation_accepts_long_cues_merged_into_one_block() -> None:
    from app.services.translation import format_timestamp

    # source 115-120 are contiguous fragments (gap <= 100ms) with run > 1200ms.
    # Gemini merges 116-118 into a single block whose time range contains them.
    source = [
        {"position": 115, "source_index": "115", "start_ms": 241600, "end_ms": 244200, "text": "a."},
        {"position": 116, "source_index": "116", "start_ms": 244200, "end_ms": 247800, "text": "b."},
        {"position": 117, "source_index": "117", "start_ms": 247800, "end_ms": 249500, "text": "c."},
        {"position": 118, "source_index": "118", "start_ms": 249600, "end_ms": 251300, "text": "d."},
        {"position": 119, "source_index": "119", "start_ms": 251400, "end_ms": 255600, "text": "e."},
        {"position": 120, "source_index": "120", "start_ms": 255600, "end_ms": 256400, "text": "f."},
    ]
    chunk = Chunk("merged", 0, 0, 5, 0, 5)
    # Merged block covering 116-118 (244200..251300) contains both long absorbed
    # cues, so it must be accepted instead of rejected as a dropped long cue.
    output = (
        f"1\n{format_timestamp(241600)} --> {format_timestamp(251300)}\nขยับเข้ามาเป็นหนึ่งชุด\n\n"
        f"2\n{format_timestamp(251400)} --> {format_timestamp(255600)}\nแล้วต่อด้วยอีกชุด\n\n"
        f"3\n{format_timestamp(255600)} --> {format_timestamp(256400)}\nแล้วก็ท้ายชุด"
    )
    cues, warnings = validate_chunk(output, source, chunk)
    covered = {p for cue in cues for p in cue["source_cue_indexes"]}
    assert {116, 117, 118} <= covered


def test_parse_model_srt_tolerates_fused_index_and_inline_text() -> None:
    # Gemini often collapses the subtitle index onto the timecode line and may
    # put the spoken text on the same line.  Both forms must parse, with the
    # time positions still aligned so validation can map them to source cues.
    raw = """1
00:00:00,000 --> 00:00:01,000
หนึ่ง

2 00:00:01,200 --> 00:00:02,000
สอง

3  00:00:02,700 --> 00:00:03,500 สาม
"""
    cues = parse_model_srt(raw)
    assert [(c["start_ms"], c["end_ms"]) for c in cues] == [
        (0, 1000),
        (1200, 2000),
        (2700, 3500),
    ]
    assert [c["text"] for c in cues] == ["หนึ่ง", "สอง", "สาม"]


def test_parse_model_srt_still_rejects_empty_cue() -> None:
    # The parser must keep enforcing structural integrity: a cue without text
    # or with an inverted time range is still an error, not silently accepted.
    with pytest.raises(TranslationError, match="ว่างหรือมีเวลาไม่ถูกต้อง"):
        parse_model_srt("1\n00:00:00,000 --> 00:00:01,000\n\n")
    with pytest.raises(TranslationError, match="ว่างหรือมีเวลาไม่ถูกต้อง"):
        parse_model_srt("1\n00:00:03,000 --> 00:00:01,000\nสวัสดี")


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
    # Lenient mode accepts the chunk (so work does not stall) but flags the
    # offending content as a warning for review.
    cues, warnings = validate_chunk(text, source_cues(), Chunk("c1", 0, 0, 1, 0, 1))
    assert len(cues) == 1
    assert warnings


def test_absurd_cue_error_flags_runaway_merge_only() -> None:
    def cue(text: str, start: int, end: int) -> dict:
        return {"text": text, "start_ms": start, "end_ms": end}

    assert absurd_cue_error([cue("สวัสดีตอนเช้า", 0, 4000)]) is None
    # Long but roomy is fine: 500 chars in 30s is unhurried speech.
    assert absurd_cue_error([cue("ก" * 500, 0, 30_000)]) is None
    # 1558 chars in 4.88s (319 chars/s) can never be spoken or fitted.
    message = absurd_cue_error([cue("ก" * 1558, 3670880, 3675760)])
    assert message is not None and "ยาวผิดปกติ" in message


def test_translate_chunk_splits_on_runaway_merge(monkeypatch, tmp_path) -> None:
    configure_temp_data(monkeypatch, tmp_path)
    db.init_db()
    db.create_video_job(
        job_id="absurd", filename="v.mp4", source_path="https://x", source_language="en",
        pause_after_transcription=False, pause_after_translation=False,
        background_volume=100, voice_volume=100,
    )
    source = [
        {"position": 1, "source_index": "1", "start_ms": 3670880, "end_ms": 3675760, "text": "Stuff."},
        {"position": 2, "source_index": "2", "start_ms": 3675760, "end_ms": 3680000, "text": "More."},
    ]
    chunk = Chunk("absurd-chunk", 0, 0, 1, 0, 1)

    class FakeModels:
        def __init__(self, text: str):
            self._text = text

        def list(self):
            return []

        def generate_content(self, **kwargs):
            return SimpleNamespace(
                text=self._text,
                usage_metadata=None,
                candidates=[SimpleNamespace(finish_reason="STOP")],
            )

    mega = "1\n01:01:10,880 --> 01:01:15,760\n" + "ก" * 1558 + "\n"
    with pytest.raises(ChunkValidationError, match="ยาวผิดปกติ"):
        _translate_chunk(
            job_id="absurd", source=source, chunk=chunk, source_language="en",
            client=SimpleNamespace(models=FakeModels(mega)), models=["m"],
        )

    normal = "1\n01:01:10,880 --> 01:01:15,760\nสวัสดีตอนเช้า\n"
    cues, _warnings, model = _translate_chunk(
        job_id="absurd", source=source, chunk=chunk, source_language="en",
        client=SimpleNamespace(models=FakeModels(normal)), models=["m"],
    )
    assert model == "m"
    assert cues[0]["text"] == "สวัสดีตอนเช้า"
