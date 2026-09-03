import pytest

from app.services.srt import SrtValidationError, parse_srt


def test_parses_thai_multiline_and_bom():
    raw = "\ufeff1\n00:00:01,000 --> 00:00:03,500\nสวัสดีครับ\nยินดีต้อนรับ\n\n2\n00:00:04,000 --> 00:00:05,000\n<b>เริ่มกันเลย</b>".encode()
    parsed = parse_srt(raw)
    assert parsed.encoding == "utf-8-sig"
    assert len(parsed.cues) == 2
    assert parsed.cues[0].text == "สวัสดีครับ ยินดีต้อนรับ"
    assert parsed.cues[1].text == "เริ่มกันเลย"
    assert parsed.cues[0].start_ms == 1000


def test_warns_for_overlap_and_missing_index():
    raw = b"00:00:00,000 --> 00:00:02,000\nFirst\n\n2\n00:00:01,500 --> 00:00:03,000\nSecond"
    parsed = parse_srt(raw)
    assert "ไม่มีหมายเลข" in parsed.cues[0].warnings[0]
    assert "ทับ" in parsed.cues[1].warnings[0]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"1\nnot a time\nText",
        b"1\n00:00:03,000 --> 00:00:02,000\nText",
        b"1\n00:00:00,000 --> 00:00:02,000\n",
    ],
)
def test_rejects_invalid_srt(raw):
    with pytest.raises(SrtValidationError):
        parse_srt(raw)


def test_detects_legacy_encoding():
    raw = "1\n00:00:00,000 --> 00:00:01,000\ncafé".encode("cp1252")
    parsed = parse_srt(raw)
    assert parsed.cues[0].text == "café"


def test_rolled_srt_without_blank_lines_splits_into_separate_cues():
    # Many tools export "rolled" SRT with no blank line between cues; the parser
    # must still treat each timecode as the start of a new cue instead of
    # collapsing the whole file into one block.
    raw = (
        "1\n00:00:01,000 --> 00:00:02,000\nสวัสดี\n"
        "2\n00:00:03,000 --> 00:00:04,000\nสวัสดีชาวโลก\n"
        "3\n00:00:05,000 --> 00:00:06,000\nยินดีต้อนรับ"
    ).encode()
    parsed = parse_srt(raw)
    assert len(parsed.cues) == 3
    assert [cue.text for cue in parsed.cues] == ["สวัสดี", "สวัสดีชาวโลก", "ยินดีต้อนรับ"]
    assert [cue.start_ms for cue in parsed.cues] == [1000, 3000, 5000]


def test_rolled_srt_keeps_multiline_text_and_leading_index():
    raw = (
        "1\n00:00:01,000 --> 00:00:05,000\nบรรทัดแรก\nบรรทัดที่สอง\n"
        "2\n00:00:06,000 --> 00:00:07,000\nถัดไป"
    ).encode()
    parsed = parse_srt(raw)
    assert len(parsed.cues) == 2
    assert parsed.cues[0].text == "บรรทัดแรก บรรทัดที่สอง"
    assert parsed.cues[1].text == "ถัดไป"


def test_rolled_srt_numeric_trailing_text_is_not_mistaken_for_index():
    # A cue whose text ends in a number must keep that number as text; only the
    # standalone numeric index line immediately before the *next* timecode is
    # treated as the index.
    raw = (
        "1\n00:00:01,000 --> 00:00:04,000\nรุ่น 2\n"
        "2\n00:00:05,000 --> 00:00:06,000\nถัดไป"
    ).encode()
    parsed = parse_srt(raw)
    assert len(parsed.cues) == 2
    assert parsed.cues[0].text == "รุ่น 2"
    assert parsed.cues[1].text == "ถัดไป"
