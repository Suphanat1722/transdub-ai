from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from ..core.config import TRANSLATION_MODELS, gemini_api_key
from ..repositories import database as db


class QuotaWait(RuntimeError):
    """Raised when an external service (Gemini) reports rate limiting; the worker waits and retries."""

    def __init__(self, message: str, retry_at: datetime):
        super().__init__(message)
        self.retry_at = retry_at

DEFAULT_TRANSLATION_PROMPT = """คุณเป็นนักแปลซับไตเติลและผู้เรียบเรียงบทพากย์ภาษาไทยมืออาชีพ งานของคุณคือแปลไฟล์ SRT ที่ได้รับเป็นภาษาไทย เพื่อนำไปสร้างเสียงพากย์ด้วยระบบ TTS

ห้ามแปลแยกทีละ cue โดยไม่ดูบริบท ให้อ่านหลาย cue ที่ต่อเนื่องกันก่อน ประกอบเป็นประโยคหรือความคิดที่สมบูรณ์ แล้วจึงแปล

---

## กฎการแปลเนื้อหา

1. รักษาความหมาย ข้อมูล น้ำเสียง และเจตนาของต้นฉบับให้ครบถ้วน ห้ามเพิ่มข้อมูลที่ไม่มีในต้นฉบับ **และใช้เวลาของแต่ละ cue ให้เต็มพอดี**: ดู timecode ของแต่ละ cue แล้วตั้งเป้าความยาวคำแปลประมาณ `วินาที × 16 ตัวอักษรไทย` — ช่องไหนต้นฉบับพูดแน่นจนเกินงบ ให้แปลกระชับแบบภาษาพูด (ตัดเฉพาะส่วนที่เกินจริง ๆ) ช่องไหนเวลาเหลือ ให้ใช้ถ้อยคำเต็มตามธรรมชาติ ห้ามยัดคำไร้ความหมาย ห้ามตัดข้อเท็จจริงหรือสาระ
2. ใช้ภาษาไทยแบบภาษาพูดที่เป็นธรรมชาติ เหมาะกับการพากย์และ TTS ไม่แปลแข็งแบบคำต่อคำ วางจุลภาค (,) ตรงรอยต่ออนุประโยคหรือจุดที่ควรเว้นจังหวะหายใจ เพราะเสียง TTS ใช้จุลภาคเป็นสัญญาณหยุด ถ้าไม่มี TTS จะเดาจุดหยุดเองและอาจหยุดผิดที่ ห้ามโรยจุลภาคทุกช่องว่าง
3. **สรรพนามและระดับภาษา**: เลือกตั้งแต่ cue แรกโดยดูจากบริบท (เพศ/บุคลิกผู้พูด น้ำเสียง กลุ่มเป้าหมาย) ถ้าบริบทไม่ชัดเจน ให้ใช้ "เรา" แบบเป็นกลางไว้ก่อน แล้วใช้คำเดิมนั้นตลอดทั้งไฟล์ ห้ามสลับไปมาโดยไม่มีเหตุผลจากต้นฉบับ (เช่น เปลี่ยนเพราะเปลี่ยนผู้พูดจริง)

## กฎการรวม/แบ่ง cue

4. รวม cue ที่เป็นส่วนของประโยคหรือความคิดเดียวกันได้ โดยใช้เวลาเริ่มของ cue แรกและเวลาจบของ cue สุดท้าย
5. **การรวมข้ามผู้พูด**: หากต้นฉบับมีสัญลักษณ์ระบุผู้พูด (เช่น ขึ้นต้นด้วย "- " หรือมี tag ชื่อผู้พูด) แม้จะปรากฏแค่บางจุดในไฟล์ ก็ให้ยึดสัญลักษณ์นั้นเป็นหลักทุกจุดที่พบ ห้ามรวม cue ข้ามผู้พูดเด็ดขาด หากต้นฉบับไม่มีสัญลักษณ์ระบุผู้พูดเลย ให้ยึดความต่อเนื่องของเนื้อหาเป็นหลักในการตัดสินใจรวม/ไม่รวม
6. แต่ละ cue ต้องเป็นช่วงคำพูดที่ฟังรู้เรื่องและสมบูรณ์ในตัวเอง ห้ามมีคำเชื่อม คำสั้น คำภาษาอังกฤษ หรือเศษประโยคอยู่เดี่ยว ๆ
7. ความยาวคำแปลต่อ cue ต้องพูดจบได้ภายในเวลาของ cue นั้นด้วยจังหวะพูดปกติ (ประมาณ 15-16 ตัวอักษรไทยต่อวินาที ตามจังหวะเสียงพากย์จริง) หากยาวเกินงบ ให้แปลกระชับขึ้นตามข้อ 1 ก่อน หากยังยาวเกินอยู่ค่อยแบ่งตามจังหวะความหมาย ห้ามแบ่งกลางวลี หากสั้นกว่างบเกินราวหนึ่งในสี่ ให้ใช้ถ้อยคำเต็มขึ้นจนพอดีช่องแทนการปล่อยว่าง
8. ห้ามสร้าง timecode ที่ซ้อนทับกัน ย้อนเวลา หรือหลุดออกนอกช่วงเวลารวมของ cue ต้นฉบับ หากต้นฉบับมี timecode ผิดปกติ (ซ้อนทับ/ซ้ำ/เรียงผิด) ให้ปรับแก้เท่าที่จำเป็นเพื่อให้เรียงลำดับถูกต้อง โดยคงช่วงเวลารวมเดิมไว้ ห้ามแต่งเนื้อหาเพิ่มเพื่อกลบช่วงเวลา

## กฎ cue ที่ไม่ใช่บทพูด

9. cue ที่มีแต่สัญลักษณ์ไม่ใช่คำพูด (เช่น [music], (laughs), ♪) และไม่มีเนื้อหาที่ต้องสื่อความหมาย ให้ลบ cue นั้นทิ้งและรวมช่วงเวลาเข้ากับ cue ข้างเคียงที่มีเนื้อหา ห้ามเหลือ cue ว่าง

## กฎการจัดการภาษาอังกฤษและศัพท์เทคนิค

10. ผลลัพธ์ต้องไม่มีตัวอักษรภาษาอังกฤษ A-Z หรือ a-z หลงเหลืออยู่เลย ทุกคำต้องแปลหรือถอดเสียงเป็นอักษรไทยที่ TTS อ่านออกเสียงได้ถูกต้อง
11. คำทั่วไปให้แปลตามความหมาย ส่วนชื่อเฉพาะ แบรนด์ บุคคล สถานที่ ผลิตภัณฑ์ หรือเทคโนโลยี ให้ถอดเสียงเป็นภาษาไทย เช่น Unity → ยูนิตี้, YouTube → ยูทูบ, Google → กูเกิล
12. ชื่อตัวแปร ฟังก์ชัน เมธอด คลาส พร็อพเพอร์ตี้ อ็อบเจ็กต์ ค่าคงที่ และ identifier ในโค้ด **ห้ามแปลความหมาย** ให้ถอดเสียงชื่อเดิมตามลำดับคำเดิมเท่านั้น เช่น:
    - `moveSpeed` → มูฟสปีด (ห้ามแปลเป็น "ความเร็วเคลื่อนที่")
    - `playerHealth` → เพลเยอร์เฮลธ์
    - `maxJumpHeight` → แม็กซ์จัมป์ไฮต์
    - `GetComponent` → เก็ตคอมโพเนนต์
    - `isGrounded` → อิสกราวน์เด็ด
13. หากคำเดียวกันไม่ได้ใช้เป็นชื่อในโค้ด แต่เป็นคำทั่วไปในประโยคปกติ ให้แปลตามความหมายตามปกติ (ไม่ต้องถอดเสียงแบบข้อ 12)
14. **สัญลักษณ์และโอเปอเรเตอร์ในโค้ดที่พูดออกเสียง** ให้แปลงเป็นคำอ่านภาษาไทยที่สื่อความหมายเดิม เช่น:
    - `==` → เท่ากับเท่ากับ / เทียบเท่ากับ, `!=` → ไม่เท่ากับ
    - `->` หรือ `=>` → ลูกศรไปยัง / ส่งต่อไปยัง
    - `%` → เปอร์เซ็นต์ หรือ มอด (ถ้าเป็น modulo), `&&` → แอนด์, `||` → ออร์
15. ตัวเลข ตัวย่อ สัญลักษณ์ และหน่วย ให้ปรับเป็นรูปแบบที่ TTS ภาษาไทยอ่านได้โดยไม่เปลี่ยนค่าหรือความหมายเดิม เช่น `10%` → สิบเปอร์เซ็นต์, `3.5` → สามจุดห้า, `ms` → มิลลิวินาที, `fps` → เฟรมต่อวินาที
16. ห้ามเขียนภาษาอังกฤษกำกับในวงเล็บ เช่น "ยูนิตี้ (Unity)" ให้เขียนเพียง "ยูนิตี้"

## กฎความสม่ำเสมอ

17. คำแปลและคำถอดเสียงของคำเดียวกัน ต้องใช้รูปแบบเดียวกันตลอดทั้งไฟล์ (เช่น ถ้าเลือกถอด `maxJumpHeight` เป็น "แม็กซ์จัมป์ไฮต์" ในครั้งแรก ต้องใช้คำเดิมนี้ทุกครั้งที่พบคำนี้อีก)
18. ลบเฉพาะ markup ที่ไม่จำเป็นต่อการออกเสียง (เช่น ป้ายกำกับสไตล์) แต่ห้ามลบข้อความที่มีความหมาย

## การตรวจทานก่อนตอบ

19. ก่อนส่งคำตอบ ให้ตรวจทานทีละ cue จริง (ไม่ใช่แค่สรุปว่าผ่าน) ว่า:
    - ไม่มีตัวอักษร A-Z / a-z หลงเหลือ
    - ไม่มี cue ว่างหรือเศษประโยคเดี่ยว ๆ
    - ไม่มี cue ยาวเกินจังหวะพูดตามข้อ 7
    - ไม่มี cue สั้นกว่างบเวลาของมันเกินราวหนึ่งในสี่
    - ไม่มี timecode ซ้อนทับหรือย้อนเวลา
    - หมายเลข cue เรียงใหม่ตั้งแต่ 1 ต่อเนื่องกันไม่ขาดตอน
    - คำถอดเสียงเดียวกันสะกดตรงกันทุกจุดที่ปรากฏในไฟล์นี้

กฎทั้งหมดนี้ใช้กับเนื้อหาทุกประเภท ไม่จำกัดเฉพาะโปรแกรมมิ่งหรือเทคโนโลยี

---

## ตัวอย่างช่องแน่น (ตัดเฉพาะส่วนที่เกินงบ ไม่ใช่มาตรฐานทุกช่อง)

ต้นฉบับ:
```
1
00:00:00,080 --> 00:00:02,320
Last year, I had $100 in my bank

2
00:00:02,320 --> 00:00:04,400
account, no previous game development or
```

คำแปลที่ถูกต้อง (กระชับ ไม่ใส่คำฟุ่มเฟือยอย่าง "ธนาคาร" ที่ซ้ำซ้อนกับ "บัญชี"):
```
1
00:00:00,080 --> 00:00:04,400
ปีที่แล้ว ผมมีเงินติดบัญชีแค่หนึ่งร้อยดอลลาร์ ไม่มีประสบการณ์พัฒนาเกมหรือเขียนโปรแกรมมาก่อนเลย
```

ตัวอย่างช่องโล่ง (ใช้ถ้อยคำเต็มตามธรรมชาติ ไม่ตัดจนโหว่):
ต้นฉบับ (ช่อง 5.5 วินาที):
```
1
00:00:00,080 --> 00:00:05,600
In this course, you will learn the basics
```

คำแปลที่ถูกต้อง (เต็มช่องด้วยคำธรรมชาติ ไม่แต่งข้อมูลเพิ่ม):
```
1
00:00:00,080 --> 00:00:05,600
ในคอร์สนี้ คุณจะได้มาเรียนรู้พื้นฐานกัน
```

---

**รูปแบบคำตอบ**: ตอบเป็นไฟล์ SRT ที่ถูกต้องตามมาตรฐานเท่านั้น ห้ามใส่คำอธิบาย ห้ามใช้ Markdown และห้ามครอบด้วย code block"""

# Gemini sometimes collapses the subtitle index and the timecode onto one line
# (e.g. ``2 00:00:02,000 --> 00:00:03,000``), and may append the spoken line to
# the same timecode line (``... --> ... หนึ่ง``).  Accept an optional leading
# index without shifting the first eight capture groups that ``_ms`` depends on,
# and capture any trailing-on-line text as group 9.
TIME_RE = re.compile(
    r"^(?:\d{1,3}\s+)?(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+(.*))?$"
)
# Thai TTS (Edge TTS) may read Latin characters embedded in a Thai sentence only
# when the user has deliberately supplied a glossary.  A bare English token,
# however, is almost always read incorrectly.  This mirrors the validator used
# by the original translator project and avoids rejecting otherwise valid Thai cues
# that contain a product or proper name in context.
STANDALONE_LATIN_RE = re.compile(
    r'''^[\s"'([{]*[A-Za-z][A-Za-z0-9#_.+\-]{0,20}[.!?]?[\s"')\]}]*$'''
)
LATIN_TOKEN_RE = re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9#_.+\-']*(?![A-Za-z])")
UNSAFE_LATIN_TOKENS = {
    "ai",
    "api",
    "cpu",
    "gpu",
    "mp3",
    "mp4",
    "obs",
    "srt",
    "tts",
    "wav",
}
MARKUP_RE = re.compile(r"</?[a-z][^>]*>|\{\\[^}]+\}|```", re.I)
THAI_FRAGMENT_TEXT = {"และ", "แต่", "หรือ", "เพราะว่า", "ซึ่ง", "ดังนั้น", "ถ้า", "เมื่อ"}

ALIGNMENT_TOLERANCE_MS = 700
SHORT_CUE_ALIGNMENT_MS = 1_000
MIN_SPLIT_CUES = 2
# A merged block beyond this is physically unpronounceable in its slot
# (~4x normal pace): accepting it poisons timing for the whole region
# (e.g. 100s of audio in a 5s slot), so the chunk must split and retry.
ABSURD_CUE_CHARS = 300
ABSURD_CUE_DENSITY = 60.0


class TranslationError(RuntimeError):
    pass


class ChunkValidationError(TranslationError):
    """All models answered, but their subtitle output failed validation."""


@dataclass(slots=True)
class Chunk:
    id: str
    index: int
    target_start: int
    target_end: int
    context_start: int
    context_end: int


def _ms(parts: tuple[str, ...]) -> int:
    h, m, s, ms = map(int, parts)
    if m > 59 or s > 59:
        raise TranslationError("timecode มีนาทีหรือวินาทีเกิน 59")
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def format_timestamp(ms: int) -> str:
    safe = max(0, int(ms))
    hours, remainder = divmod(safe, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def serialize_srt(cues: list[dict], bom: bool = False) -> str:
    body = "\r\n\r\n".join(
        f"{index}\r\n{format_timestamp(int(cue['start_ms']))} --> "
        f"{format_timestamp(int(cue['end_ms']))}\r\n{str(cue['text']).strip()}"
        for index, cue in enumerate(cues, 1)
    )
    return ("\ufeff" if bom else "") + body + "\r\n"


def parse_model_srt(raw: str) -> list[dict]:
    cleaned = raw.replace("\ufeff", "").strip()
    cleaned = re.sub(r"^```(?:srt|text)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    blocks = re.split(r"\n\s*\n", cleaned.replace("\r\n", "\n").replace("\r", "\n"))
    cues: list[dict] = []
    for block_number, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block.split("\n")]
        time_index = next((index for index, line in enumerate(lines) if TIME_RE.match(line)), -1)
        if time_index < 0:
            raise TranslationError(f"ไม่พบ timecode ในผลแปลบล็อก {block_number}")
        match = TIME_RE.match(lines[time_index])
        assert match is not None
        start_ms = _ms(match.groups()[:4])
        end_ms = _ms(match.groups()[4:8])
        # Text may share the timecode line (``... --> ... หนึ่ง``) instead of
        # starting on the following line.  Join both so nothing is dropped.
        trailing = (match.group(9) or "").strip()
        text = " ".join(
            line
            for line in [trailing, *lines[time_index + 1 :]]
            if line.strip()
        ).strip()
        if not text or end_ms <= start_ms:
            raise TranslationError(f"ผลแปล cue {block_number} ว่างหรือมีเวลาไม่ถูกต้อง")
        cues.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
    return cues


def _safe_boundary(cues: list[dict], index: int) -> bool:
    if index + 1 >= len(cues):
        return True
    return bool(re.search(r"[.!?…。！？][\"'”’)]*$", cues[index]["text"])) or (
        int(cues[index + 1]["start_ms"]) - int(cues[index]["end_ms"]) >= 800
    )


def build_chunks(job_id: str, cues: list[dict]) -> list[Chunk]:
    chunks: list[Chunk] = []
    start = 0
    while start < len(cues):
        end = start
        characters = 0
        last_safe = -1
        while end < len(cues):
            characters += len(cues[end]["text"])
            count = end - start + 1
            if _safe_boundary(cues, end):
                last_safe = end
            hard = count >= 220 or characters >= 32_000
            target = count >= 150 or characters >= 24_000
            if hard:
                if last_safe >= start + 90:
                    end = last_safe
                break
            if target and _safe_boundary(cues, end):
                break
            end += 1
        end = min(end, len(cues) - 1)
        chunks.append(
            Chunk(
                id=f"{job_id}-chunk-{len(chunks) + 1:04d}",
                index=len(chunks),
                target_start=start,
                target_end=end,
                context_start=max(0, start - 4),
                context_end=min(len(cues) - 1, end + 4),
            )
        )
        start = end + 1
    return chunks


def _reindex_chunks(chunks: list[Chunk]) -> None:
    for index, chunk in enumerate(chunks):
        chunk.index = index


def absurd_cue_error(cues: list[dict]) -> str | None:
    """Describe the first physically impossible cue, if any.

    A runaway merge (hundreds of characters in a few-second slot) can never be
    spoken or fitted; accepting it poisons downstream timing for the whole
    region.  Returns an error message so the chunk splits and retries smaller
    instead of flowing through with a warning nobody can act on.
    """
    for position, cue in enumerate(cues, 1):
        dense = len(re.sub(r"[\s\W]", "", cue["text"]))
        seconds = max(0.001, (cue["end_ms"] - cue["start_ms"]) / 1000)
        if dense > ABSURD_CUE_CHARS and dense / seconds > ABSURD_CUE_DENSITY:
            return f"cue {position} ยาวผิดปกติ ({dense} ตัวอักษรใน {seconds:.1f} วินาที)"
    return None


def split_chunk(chunk: Chunk, source: list[dict]) -> tuple[Chunk, Chunk] | None:
    """Split a failed translation request at a natural source-cue boundary.

    Gemini occasionally omits short cues when a request is very large.  The
    worker must be able to retry the same work at a smaller granularity without
    changing source order or losing the surrounding context.  A sentence/gap
    boundary is preferred; a midpoint is used as a deterministic last resort.
    """
    if chunk.target_end - chunk.target_start + 1 < MIN_SPLIT_CUES:
        return None

    middle = (chunk.target_start + chunk.target_end) // 2
    boundary: int | None = None
    max_distance = min(20, (chunk.target_end - chunk.target_start) // 2)
    for distance in range(max_distance + 1):
        candidates = [middle - distance]
        if distance:
            candidates.append(middle + distance)
        for candidate in candidates:
            if chunk.target_start <= candidate < chunk.target_end and _safe_boundary(source, candidate):
                boundary = candidate
                break
        if boundary is not None:
            break
    if boundary is None:
        boundary = middle

    def child(suffix: str, start: int, end: int) -> Chunk:
        return Chunk(
            id=f"{chunk.id}-{suffix}",
            index=chunk.index,
            target_start=start,
            target_end=end,
            context_start=max(0, start - 4),
            context_end=min(len(source) - 1, end + 4),
        )

    return child("a", chunk.target_start, boundary), child("b", boundary + 1, chunk.target_end)


def _render(cues: list[dict], start: int, end: int) -> str:
    if start > end:
        return "(ไม่มี)"
    return "\n\n".join(
        f"{cue['source_index']}\n{format_timestamp(cue['start_ms'])} --> "
        f"{format_timestamp(cue['end_ms'])}\n{cue['text']}"
        for cue in cues[start : end + 1]
    )


def _request(
    cues: list[dict], chunk: Chunk, source_language: str, errors: list[str], prompt: str | None = None
) -> tuple[str, str, int]:
    target_chars = sum(len(cue["text"]) for cue in cues[chunk.target_start : chunk.target_end + 1])
    correction = ""
    if errors:
        correction = "\n\nผลก่อนหน้ามีข้อผิดพลาด โปรดสร้างใหม่ทั้งหมด:\n- " + "\n- ".join(errors[:12])
    contents = _render(cues, chunk.target_start, chunk.target_end) + correction
    system = (prompt or "").strip() or DEFAULT_TRANSLATION_PROMPT
    return system, contents, min(32_768, max(4_096, int(target_chars * 1.2)))


def _interval_gap(left: dict, right: dict) -> int:
    """Return the distance between two intervals, or zero when they overlap."""
    if int(left["end_ms"]) <= int(right["start_ms"]):
        return int(right["start_ms"]) - int(left["end_ms"])
    if int(right["end_ms"]) <= int(left["start_ms"]):
        return int(left["start_ms"]) - int(right["end_ms"])
    return 0


def _alignment_limit(source_cue: dict) -> int:
    """Allow a little more drift for the short cues models tend to absorb."""
    duration = int(source_cue["end_ms"]) - int(source_cue["start_ms"])
    return SHORT_CUE_ALIGNMENT_MS if duration <= 1_200 else ALIGNMENT_TOLERANCE_MS // 2


def _interval_contains(outer: dict, inner: dict) -> bool:
    """True when ``inner`` falls fully inside ``outer``'s time range.

    Merging is the one signal that is unambiguous: a source cue that lies
    inside a single translated cue's interval was absorbed into that cue
    (not omitted), so acknowledging it never hides a genuinely dropped line.
    """
    return int(outer["start_ms"]) <= int(inner["start_ms"]) and int(inner["end_ms"]) <= int(outer["end_ms"])


def _can_fallback_align(source_cue: dict, gap: int, *, contained_in_translation: bool = False) -> bool:
    """Allow a source cue to share a translated cue when they are adjacent.

    A standalone long cue that was dropped entirely must still fail, but a long
    cue absorbed into one merged translated line's time range should not.  The
    ``contained_in_translation`` signal distinguishes those two cases even when
    the boundaries touch exactly (gap == 0).
    """
    if contained_in_translation:
        return True
    duration = int(source_cue["end_ms"]) - int(source_cue["start_ms"])
    if duration > 1_200 and gap == 0:
        return False
    return gap <= _alignment_limit(source_cue) and (duration <= 1_200 or gap > 0)


def _has_unsafe_latin(text: str) -> bool:
    stripped = text.strip()
    if STANDALONE_LATIN_RE.fullmatch(stripped):
        return True
    if LATIN_TOKEN_RE.search(stripped) and not re.search(r"[\u0E00-\u0E7F]", stripped):
        return True
# Proper names such as “Whisper Flow” are useful in a Thai sentence and
    # are handled by the voice glossary.  Short all-capital technical tokens
    # (AI/API/MP4/...) are not reliably pronounced by TTS without a glossary.
    return any(
        token.lower().rstrip("'") in UNSAFE_LATIN_TOKENS
        for token in LATIN_TOKEN_RE.findall(text)
    )


def validate_chunk(
    raw: str, source: list[dict], chunk: Chunk, *, strict: bool = False
) -> tuple[list[dict], list[str]]:
    parsed = sorted(parse_model_srt(raw), key=lambda cue: (cue["start_ms"], cue["end_ms"]))
    normalized: list[dict] = []
    warnings: list[str] = []
    for cue in parsed:
        if normalized and cue["start_ms"] < normalized[-1]["end_ms"]:
            if cue["end_ms"] <= normalized[-1]["end_ms"]:
                normalized[-1]["text"] += " " + cue["text"]
                warnings.append("รวม cue ที่อยู่ภายในช่วงก่อนหน้า")
                continue
            cue["start_ms"] = normalized[-1]["end_ms"]
            warnings.append("ปรับเวลาเริ่ม cue ที่ซ้อนกัน")
        normalized.append(cue)
    range_start = source[chunk.target_start]["start_ms"]
    range_end = source[chunk.target_end]["end_ms"]
    errors: list[str] = []
    results: list[dict] = []
    target = source[chunk.target_start : chunk.target_end + 1]
    for index, cue in enumerate(normalized, 1):
        alignment_warnings: list[str] = []
        indexes = [
            item["position"]
            for item in target
            if item["start_ms"] < cue["end_ms"] and item["end_ms"] > cue["start_ms"]
        ]
        if not indexes and target:
            nearest = min(target, key=lambda item: _interval_gap(cue, item))
            gap = _interval_gap(cue, nearest)
            if _can_fallback_align(nearest, gap, contained_in_translation=_interval_contains(cue, nearest)):
                indexes = [nearest["position"]]
                alignment_warnings.append(
                    f"จับคู่ cue {index} กับ cue ต้นฉบับ {nearest['source_index']} "
                    f"แม้เวลาเลื่อน {gap} ms"
                )
                warnings.append(alignment_warnings[-1])
        if cue["start_ms"] < range_start or cue["end_ms"] > range_end:
            errors.append(f"cue {index} ออกนอกช่วงต้นฉบับ")
        if not indexes:
            errors.append(f"cue {index} ไม่ตรงกับต้นฉบับ")
        if _has_unsafe_latin(cue["text"]):
            errors.append(f"cue {index} ยังมีอักษรอังกฤษ")
        if MARKUP_RE.search(cue["text"]):
            errors.append(f"cue {index} ยังมี markup")
        if cue["text"].strip() in THAI_FRAGMENT_TEXT:
            errors.append(f"cue {index} เป็นเศษประโยค")
        duration = max(1, cue["end_ms"] - cue["start_ms"])
        cue_warnings = []
        if len(re.sub(r"[\s\W]", "", cue["text"])) / (duration / 1000) > 17:
            cue_warnings.append("เร็วกว่า 17 อักขระต่อวินาที")
        cue_warnings.extend(alignment_warnings)
        if LATIN_TOKEN_RE.search(cue["text"]) and not _has_unsafe_latin(cue["text"]):
            cue_warnings.append("มีชื่อเฉพาะ/คำละติน อาจต้องตรวจเสียงอ่าน")
        results.append(
            {
                **cue,
                "source_index": index,
                "source_cue_indexes": indexes,
                "warnings": cue_warnings,
                "translation_chunk_id": chunk.id,
            }
        )
    covered = {position for result in results for position in result["source_cue_indexes"]}
    for source_cue in target:
        if source_cue["position"] in covered or not results:
            continue
        nearest_index, nearest_result = min(
            enumerate(results), key=lambda item: _interval_gap(item[1], source_cue)
        )
        gap = _interval_gap(nearest_result, source_cue)
        if _can_fallback_align(
            source_cue, gap, contained_in_translation=_interval_contains(nearest_result, source_cue)
        ):
            nearest_result["source_cue_indexes"].append(source_cue["position"])
            covered.add(source_cue["position"])
            warning = (
                f"จับคู่ cue ต้นฉบับ {source_cue['source_index']} กับ cue แปล {nearest_index + 1} "
                f"ด้วย tolerance {gap} ms"
            )
            nearest_result["warnings"].append(warning)
            warnings.append(warning)
    for cue in target:
        if not any(cue["position"] in result["source_cue_indexes"] for result in results):
            errors.append(f"cue ต้นฉบับ {cue['source_index']} ไม่ถูกครอบคลุม")
    if errors:
        if strict:
            raise TranslationError("; ".join(dict.fromkeys(errors)))
        # Non-strict mode: the translated chunk is still usable.  Surface the
        # mapping gaps as warnings so the work can proceed and be reviewed in
        # the UI instead of rejecting the whole chunk and exhausting every model.
        for message in dict.fromkeys(errors):
            warnings.append(message)
    return results, warnings


def _usage(metadata: Any) -> dict:
    def value(*names: str) -> int:
        for name in names:
            candidate = getattr(metadata, name, None)
            if isinstance(candidate, int):
                return candidate
        return 0

    return {
        "input": value("prompt_token_count", "input_token_count"),
        "output": value("candidates_token_count", "output_token_count"),
        "thoughts": value("thoughts_token_count", "thought_token_count"),
        "total": value("total_token_count", "total_tokens"),
    }


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(response, "finish_reason", None)
    if candidates:
        reason = getattr(candidates[0], "finish_reason", reason)
    return str(reason or "").upper()


def available_models(client: Any) -> list[str]:
    try:
        names = {
            str(getattr(model, "name", "")).replace("models/", "")
            for model in client.models.list()
        }
        selected = [model for model in TRANSLATION_MODELS if model in names]
        return selected or list(TRANSLATION_MODELS)
    except Exception:
        return list(TRANSLATION_MODELS)


def _checkpoint_for(result_dir: Path, chunk: Chunk) -> Path | None:
    """Find a checkpoint from the current format or the pre-split legacy format."""
    current = result_dir / f"{chunk.id}.json"
    if current.is_file():
        return current
    legacy = result_dir / f"{chunk.index:04d}.json"
    return legacy if legacy.is_file() else None


def affected_chunk_ids_for_source_index(job_id: str, source_index: int) -> list[str]:
    """Return translation chunk ids whose target range covers a source index.

    Used by cue editing to invalidate only the affected chunk checkpoints
    instead of wiping the whole translation layer when only text changed.
    """
    try:
        return [
            str(row["id"])
            for row in db.translation_chunks(job_id)
            if int(row["target_start"]) <= source_index <= int(row["target_end"])
        ]
    except (KeyError, TypeError, ValueError):
        return []


def _translation_plan(job_id: str, source: list[dict]) -> list[Chunk]:
    """Resume a persisted split plan when the process was restarted mid-translation."""
    existing = db.translation_chunks(job_id)
    if existing and source:
        ordered: list[Chunk] = []
        for row in existing:
            try:
                ordered.append(
                    Chunk(
                        id=str(row["id"]),
                        index=int(row["chunk_index"]),
                        target_start=int(row["target_start"]),
                        target_end=int(row["target_end"]),
                        context_start=int(row["context_start"]),
                        context_end=int(row["context_end"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                ordered = []
                break
        if ordered:
            ordered.sort(key=lambda item: item.index)
            covers_source = (
                ordered[0].target_start == 0
                and ordered[-1].target_end == len(source) - 1
                and all(
                    current.target_start == previous.target_end + 1
                    for previous, current in zip(ordered, ordered[1:], strict=False)
                )
                and all(
                    0 <= item.target_start <= item.target_end < len(source) for item in ordered
                )
            )
            if covers_source:
                _reindex_chunks(ordered)
                return ordered
    return build_chunks(job_id, source)


def _translate_chunk(
    *,
    job_id: str,
    source: list[dict],
    chunk: Chunk,
    source_language: str,
    client: Any,
    models: list[str],
    prompt: str | None = None,
) -> tuple[list[dict], list[str], str]:
    """Generate and validate one chunk, trying every configured model."""
    last_error = "แปลไม่สําเร็จ"
    had_validation_error = False
    saw_quota_exhausted = False
    for model in models:
        correction_errors: list[str] = []
        db.update_job(job_id, translation_model=model)
        for attempt in range(2):
            system, contents, max_tokens = _request(
                source, chunk, source_language, correction_errors, prompt
            )
            try:
                level = types.ThinkingLevel.MINIMAL if "lite" in model else types.ThinkingLevel.LOW
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config={
                        "system_instruction": system,
                        "thinking_config": {"thinking_level": level},
                        "max_output_tokens": max_tokens,
                    },
                )
                usage = _usage(getattr(response, "usage_metadata", None))
                db.record_api_usage(job_id, "translation", model, total_tokens=usage["total"])
                if any(marker in _finish_reason(response) for marker in ("MAX_TOKENS", "LENGTH")):
                    last_error = "ผลลัพธ์ถูกตัดเพราะยาวเกินไป"
                    had_validation_error = True
                    db.record_attempt(
                        job_id,
                        "translation",
                        "invalid",
                        unit_id=chunk.id,
                        model=model,
                        message=last_error,
                        usage=usage,
                    )
                    raise ChunkValidationError(last_error)
            except ChunkValidationError:
                raise
            except Exception as exc:
                text = str(exc)
                last_error = text[-1_200:]
                db.record_attempt(
                    job_id,
                    "translation",
                    "error",
                    unit_id=chunk.id,
                    model=model,
                    message=last_error,
                )
                if "429" in text or "resource_exhausted" in text.lower():
                    # 429 is project-wide quota, not specific to this model.
                    # Break out and try the remaining (cheaper) models in the
                    # chain first — the lite model may still have quota left.
                    saw_quota_exhausted = True
                # A quota error cannot be fixed by a correction retry here, and
                # an unavailable model cannot recover in-process either.
                break

            try:
                cues, warnings = validate_chunk(response.text or "", source, chunk, strict=False)
            except TranslationError as validation_error:
                had_validation_error = True
                last_error = str(validation_error)
                db.record_attempt(
                    job_id,
                    "translation",
                    "invalid",
                    unit_id=chunk.id,
                    model=model,
                    message=last_error,
                    usage=usage,
                )
                correction_errors = last_error.split("; ")
                if attempt == 0:
                    continue
                break
            except Exception as exc:
                last_error = str(exc)[-1_200:]
                db.record_attempt(
                    job_id,
                    "translation",
                    "error",
                    unit_id=chunk.id,
                    model=model,
                    message=last_error,
                )
                break
            absurd = absurd_cue_error(cues)
            if absurd is not None:
                db.update_translation_chunk(chunk.id, status="failed", error=absurd)
                db.record_attempt(
                    job_id,
                    "translation",
                    "invalid",
                    unit_id=chunk.id,
                    model=model,
                    message=absurd,
                )
                raise ChunkValidationError(absurd)
            return cues, warnings, model
        time.sleep(1)
    if saw_quota_exhausted:
        raise QuotaWait(
            "Gemini จํากัดอัตราการแปล จะลองใหม่ภายหลัง",
            datetime.now(UTC) + timedelta(minutes=2),
        )
    error = f"ลองครบทุกโมเดลแล้ว: {last_error}"
    if had_validation_error:
        raise ChunkValidationError(error)
    raise TranslationError(error)


def translate(
    job_id: str, source: list[dict], work_dir: Path, source_language: str, prompt: str | None = None
) -> list[dict]:
    key = gemini_api_key()
    if not key:
        raise TranslationError("ไม่พบ GEMINI_API_KEY ในไฟล์ .env")
    pending = _translation_plan(job_id, source)
    db.sync_translation_chunks(job_id, [asdict(chunk) for chunk in pending])
    result_dir = work_dir / "translation"
    result_dir.mkdir(parents=True, exist_ok=True)
    translated: list[dict] = []
    timeout_seconds = 90
    with genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
    ) as client:
        models = available_models(client)
        position = 0
        while position < len(pending):
            chunk = pending[position]
            checkpoint = _checkpoint_for(result_dir, chunk)
            if checkpoint is not None:
                try:
                    completed = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if not isinstance(completed, list):
                        raise ValueError("รูปแบบ checkpoint ไม่ใช่รายการ cue")
                    translated.extend(completed)
                    db.update_translation_chunk(
                        chunk.id,
                        status="completed",
                        output_cue_count=len(completed),
                        error=None,
                    )
                    position += 1
                    continue
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    # A truncated checkpoint must not permanently block a retry.
                    checkpoint.unlink(missing_ok=True)
                    db.update_translation_chunk(chunk.id, status="pending", error=str(exc))

            try:
                cues, warnings, model = _translate_chunk(
                    job_id=job_id,
                    source=source,
                    chunk=chunk,
                    source_language=source_language,
                    client=client,
                    models=models,
                    prompt=prompt,
                )
            except ChunkValidationError as error:
                children = split_chunk(chunk, source)
                if children is not None:
                    pending[position : position + 1] = list(children)
                    _reindex_chunks(pending)
                    db.sync_translation_chunks(job_id, [asdict(item) for item in pending])
                    db.record_attempt(
                        job_id,
                        "translation",
                        "split",
                        unit_id=chunk.id,
                        message=f"แบ่ง chunk ที่แปลไม่ผ่านเป็น {children[0].id} และ {children[1].id}",
                    )
                    continue
                db.update_translation_chunk(chunk.id, status="failed", error=str(error))
                raise
            except TranslationError as error:
                db.update_translation_chunk(chunk.id, status="failed", error=str(error))
                raise

            checkpoint = result_dir / f"{chunk.id}.json"
            checkpoint.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
            translated.extend(cues)
            db.update_translation_chunk(
                chunk.id,
                status="completed",
                model=model,
                output_cue_count=len(cues),
                error=None,
            )
            db.record_attempt(
                job_id,
                "translation",
                "success",
                unit_id=chunk.id,
                model=model,
                message="; ".join(warnings) or None,
            )
            position += 1
    translated.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"]))
    for index, cue in enumerate(translated, 1):
        cue["source_index"] = index
    return translated
