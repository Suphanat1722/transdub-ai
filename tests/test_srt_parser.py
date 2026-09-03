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
