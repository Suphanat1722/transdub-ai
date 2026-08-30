import pytest

from app.services.speech_generation import (
    GENERATION_END_MARGIN_SECONDS,
    count_spoken_characters,
    estimate_total_duration_seconds,
    legacy_byte_timing_ratio,
    needs_mixed_script_duration_retry,
)


def test_unicode_units_do_not_penalize_english_inside_thai_text():
    reference = (
        "สวัสดีครับ วันนี้อากาศค่อนข้างดี ผมกำลังทดสอบระบบสร้างเสียงพากย์ภาษาไทย เพื่อให้เสียงฟังเป็นธรรมชาติและชัดเจนมากที่สุด"
    )
    generated = "Parameter คือข้อมูลที่คุณส่งเข้าไปใน function"
    reference_duration_seconds = 8.7
    total_seconds = estimate_total_duration_seconds(
        reference_duration_seconds, reference, generated, 1.0, 1.10
    )
    generated_seconds = total_seconds - reference_duration_seconds
    legacy_byte_seconds = (
        reference_duration_seconds / len(reference.encode("utf-8")) * len(generated.encode("utf-8"))
    )
    assert count_spoken_characters(generated) == len(generated.replace(" ", ""))
    assert generated_seconds > legacy_byte_seconds * 1.25
    assert legacy_byte_timing_ratio(reference, generated) < 0.90
    assert needs_mixed_script_duration_retry(reference, generated)
    assert not needs_mixed_script_duration_retry(reference, "นี่คือข้อความภาษาไทยตามปกติ")


def test_duration_estimator_validates_speed_and_multiplier():
    for reference_seconds, speed, multiplier in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
        try:
            estimate_total_duration_seconds(reference_seconds, "อ้างอิง", "ทดสอบ", speed, multiplier)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid duration settings must be rejected")


def test_duration_estimator_reserves_an_articulation_margin_for_short_text():
    total = estimate_total_duration_seconds(10.0, "ข้อความอ้างอิงที่ยาวกว่า", "สั้น", 1.0, 1.0)
    proportional = 10.0 * count_spoken_characters("สั้น") / count_spoken_characters("ข้อความอ้างอิงที่ยาวกว่า")
    assert total - 10.0 == pytest.approx(proportional + GENERATION_END_MARGIN_SECONDS)
