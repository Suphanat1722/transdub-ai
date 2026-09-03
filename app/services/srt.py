"""Strict SRT parsing and validation."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from charset_normalizer import from_bytes

TIME_RE = re.compile(
    r"^(?P<sh>\d{1,3}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})\s*-->\s*"
    r"(?P<eh>\d{1,3}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})(?:\s+.*)?$"
)
TAG_RE = re.compile(r"<[^>]*>")
ASS_TAG_RE = re.compile(r"\{\\[^}]*}")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True)
class ParsedCue:
    position: int
    source_index: str
    start_ms: int
    end_ms: int
    text: str
    warnings: list[str]


@dataclass(slots=True)
class ParsedSrt:
    encoding: str
    cues: list[ParsedCue]
    warnings: list[str]


class SrtValidationError(ValueError):
    pass


def _to_ms(h: str, m: str, s: str, ms: str) -> int:
    minute, second = int(m), int(s)
    if minute > 59 or second > 59:
        raise SrtValidationError("นาทีหรือวินาทีใน timecode เกิน 59")
    return ((int(h) * 60 + minute) * 60 + second) * 1000 + int(ms)


def decode_srt(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        match = from_bytes(data).best()
        if match is None or match.encoding is None or getattr(match, "percent_chaos", 100) > 30:
            raise SrtValidationError("ไม่สามารถตรวจหา encoding ของไฟล์ได้") from None
        decoded = str(match)
        if "\ufffd" in decoded:
            raise SrtValidationError("ไฟล์มีอักขระที่อ่านไม่ได้") from None
        return decoded, match.encoding


def parse_srt(data: bytes, lenient: bool = False) -> ParsedSrt:
    """Parse an SRT byte string into cues.

    ``lenient=True`` skips cue blocks that carry no spoken text (common in
    auto-generated on-YouTube captions, where a block can be an empty caption)
    instead of raising, while still flagging genuinely malformed cues.  Real
    interval overlap between cues is likewise taken for granted in auto-captions
    (each ASR window reaches into the next) and is normalized here by clamping
    each cue's end to the start of the next cue.  Strict mode (default) is used
    for user-uploaded SRT files.
    """
    text, encoding = decode_srt(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise SrtValidationError("ไฟล์ SRT ว่าง")

    # Group lines into cue blocks using a timecode line as the boundary.  A cue
    # does not need a blank line to separate it: many tools export "rolled" SRT
    # with no blank line between cues, which a blank-line split would collapse
    # into a single cue.  A purely numeric line immediately before a timecode is
    # detached as that cue's index; anything else stays as text.  For
    # standards-conforming files this produces the same block layout as the
    # former blank-line split.
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if line and TIME_RE.match(line):
            # A timecode line starts a new cue.  A trailing purely-numeric line
            # is that cue's index, so detach it; if the rest of ``current``
            # holds a complete cue (it already contains a timecode), close it.
            # A leading index before the *first* timecode (rolled SRT has no
            # blank line to separate it) is kept as this cue's own start.
            index_line = ""
            if current and current[-1].strip().isdigit():
                index_line = current.pop()
            if current and any(TIME_RE.match(item.strip()) for item in current):
                if any(item.strip() for item in current):
                    blocks.append(current)
                current = [index_line] + [line] if index_line else [line]
            elif index_line and not any(item.strip() for item in current):
                current = [index_line, line]
            else:
                if current and any(item.strip() for item in current):
                    blocks.append(current)
                current = [line]
        else:
            current.append(raw_line)
    if current and any(item.strip() for item in current):
        blocks.append(current)

    cues: list[ParsedCue] = []
    global_warnings: list[str] = []
    previous_end = -1
    previous_start = -1
    seen_indices: set[str] = set()
    numeric_indices: list[int] = []

    for block_no, block in enumerate(blocks, 1):
        lines = [line.strip() for line in block if line.strip()]
        if not any(lines):
            continue
        time_line = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_line is None:
            raise SrtValidationError(f"บล็อก {block_no}: ไม่พบ timecode")
        source_index = lines[0] if time_line > 0 else str(block_no)
        if source_index in seen_indices:
            raise SrtValidationError(f"บล็อก {block_no}: หมายเลข cue {source_index} ซ้ำ")
        seen_indices.add(source_index)
        if source_index.isdigit():
            numeric_indices.append(int(source_index))
        match = TIME_RE.match(lines[time_line])
        if not match:
            raise SrtValidationError(f"บล็อก {block_no}: timecode ไม่ถูกต้อง")
        values = match.groupdict()
        start = _to_ms(values["sh"], values["sm"], values["ss"], values["sms"])
        end = _to_ms(values["eh"], values["em"], values["es"], values["ems"])
        if end <= start:
            raise SrtValidationError(f"บล็อก {block_no}: เวลาจบต้องมากกว่าเวลาเริ่ม")
        spoken = " ".join(line for line in lines[time_line + 1 :] if line).strip()
        if CONTROL_RE.search(spoken):
            raise SrtValidationError(f"บล็อก {block_no}: มี control character ที่ไม่อนุญาต")
        spoken = html.unescape(TAG_RE.sub("", ASS_TAG_RE.sub("", spoken))).strip()
        if not spoken:
            if not lenient:
                raise SrtValidationError(f"บล็อก {block_no}: ไม่มีข้อความ")
            global_warnings.append(f"Cue {source_index} ถูกข้ามเพราะไม่มีข้อความ")
            continue
        cue_warnings: list[str] = []
        # YouTube auto-captions give each cue an end time that reaches past the
        # next cue's start (the ASR word window overlaps the following word).
        # That is native auto-caption timing, not a real subtitle defect, so in
        # lenient mode it is not reported; after parsing we clamp it away (see
        # below).  Strict mode (user-uploaded SRT) still flags genuine overlap.
        if not lenient and start < previous_end:
            cue_warnings.append("ช่วงเวลาทับกับ cue ก่อนหน้า")
            global_warnings.append(f"Cue {source_index} มีช่วงเวลาทับกัน")
        if start < previous_start:
            cue_warnings.append("เวลาเริ่มเรียงย้อนหลัง")
            global_warnings.append(f"Cue {source_index} มีเวลาเริ่มย้อนหลัง")
        if time_line == 0:
            cue_warnings.append("ไม่มีหมายเลข cue; ระบบกําหนดตําแหน่งให้อัตโนมัติ")
        cues.append(ParsedCue(len(cues) + 1, source_index, start, end, spoken, cue_warnings))
        previous_end = max(previous_end, end)
        previous_start = start

    if not cues:
        raise SrtValidationError("ไม่พบ subtitle cue")
    if lenient:
        # Clamp each auto-caption's end so it finishes where the next cue starts,
        # matching youtube-transcript-api output (which applies the same rule).
        for i, cue in enumerate(cues):
            if i + 1 >= len(cues):
                break
            next_start = cues[i + 1].start_ms
            if next_start > cue.start_ms and cue.end_ms > next_start:
                cue.end_ms = next_start
    if numeric_indices and numeric_indices != list(
        range(numeric_indices[0], numeric_indices[0] + len(numeric_indices))
    ):
        global_warnings.append("หมายเลข cue ไม่เรียงต่อเนื่อง")
    return ParsedSrt(encoding, cues, global_warnings)
