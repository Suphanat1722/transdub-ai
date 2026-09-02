"""Speech synthesis through Microsoft Edge TTS.

Edge TTS calls a public web endpoint (``speech.platform.bing.com``) and returns
preset neural voices only; there is no voice cloning.  Synthesis runs in-process
and fully asynchronously, so there is no dedicated model process, no GPU, and no
voice-profile reference audio.  Each cue is synthesized on demand as a 24 kHz
mono WAV placed directly in the job's cue directory, then fitted into the
timeline exactly as before.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from .audio import AudioError

# Edge TTS streams the result as an MP3 which we convert to the same 24 kHz mono
# WAV that the rest of the assembly pipeline expects (PCM s16le, SAMPLE_RATE,
# single channel).
EDGE_TTS_SAMPLE_RATE = 24_000


class EdgeTTSUnavailableError(RuntimeError):
    """Raised when ``edge_tts`` is not installed or cannot reach its endpoint."""


def list_voices() -> list[dict]:
    """Return preset Edge voices (ShortName, Locale, Gender). Reaches the network."""
    try:
        import edge_tts
    except ImportError as exc:
        raise EdgeTTSUnavailableError("ยังไม่ได้ติดตั้ง edge-tts") from exc

    def fetch() -> list[dict]:
        return [dict(voice) for voice in asyncio.run(edge_tts.list_voices())]

    try:
        return fetch()
    except Exception as exc:
        raise EdgeTTSUnavailableError(f"เรียกข้อมูลเสียงจาก Edge TTS ไม่สําเร็จ: {exc}") from exc


def rate_kwargs(rate_percent: int) -> dict[str, str]:
    """Translate a signed integer percentage into edge-tts ``rate`` syntax."""
    return {"rate": f"{rate_percent:+d}%"} if rate_percent else {}


def synth_cue(text: str, voice: str, rate_percent: int, output: Path, timeout: float = 60) -> int:
    """Synthesize ``text`` into a 24 kHz mono WAV at ``output`` and return ms.

    Fits the Edge TTS product model: synthesize to a temp MP3 via edge-tts, then
    convert to the canonical WAV with FFmpeg so downstream code is unchanged.
    """
    try:
        import edge_tts
    except ImportError as exc:
        raise EdgeTTSUnavailableError("ยังไม่ได้ติดตั้ง edge-tts") from exc

    ffmpeg = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if ffmpeg.returncode != 0:
        raise AudioError("ไม่พบ FFmpeg ใน PATH")

    output.parent.mkdir(parents=True, exist_ok=True)
    mp3_path = output.with_suffix(".mp3")

    async def generate() -> None:
        # edge-tts accepts ``rate``/``pitch`` keyword strings; its stubs are
        # too narrow to express the rate string, so ignore the arg-type here.
        communicate = edge_tts.Communicate(text, voice, **rate_kwargs(rate_percent))  # type: ignore[arg-type]
        await asyncio.wait_for(communicate.save(str(mp3_path)), timeout=timeout)

    try:
        asyncio.run(generate())
    except TimeoutError as exc:
        raise RuntimeError(f"Edge TTS สร้างเสียงเกินกว่ากําหนด ({timeout}s)") from exc
    except Exception as exc:
        raise RuntimeError(f"Edge TTS สร้างเสียงไม่สําเร็จ: {exc}") from exc
    finally:
        if not mp3_path.is_file():
            mp3_path.unlink(missing_ok=True)

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(mp3_path),
            "-ac", "1", "-ar", str(EDGE_TTS_SAMPLE_RATE), "-c:a", "pcm_s16le", str(output),
        ],
        capture_output=True,
        text=True,
    )
    mp3_path.unlink(missing_ok=True)
    if result.returncode:
        raise AudioError(result.stderr.strip() or "FFmpeg แปลงเสียงของ Edge TTS ไม่สําเร็จ")
    if not output.is_file():
        raise RuntimeError("Edge TTS ไม่ได้สร้างไฟล์เสียง")
    return _duration_ms(output)


def _duration_ms(path: Path) -> int:
    import soundfile as sf

    info = sf.info(str(path))
    return int(info.frames * 1000 / info.samplerate)