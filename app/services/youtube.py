"""Download a YouTube video and fetch its on-Youtube subtitles.

The pipeline entry point is a YouTube URL: the actual video file (needed for
Demucs separation and muxing) is downloaded with yt-dlp, and the subtitles are
read straight from YouTube with yt-dlp as well.  Thai subtitles become
the dub text directly; other languages are sent to Gemini for translation.

Neither call needs a YouTube API key.
"""

from __future__ import annotations

import re
import tempfile
from contextlib import suppress
from pathlib import Path

from ..core.config import youtube_proxy_settings

# Languages chosen directly as the final dub text (no Gemini translation).
THAI_LANGUAGE = "th"
# Preferred subtitle language order; the first available one wins.
PREFERRED_LANGUAGES = ("th", "en")


class YouTubeError(RuntimeError):
    """An error downloading a video or fetching subtitles, safe to show in the UI."""


def extract_video_id(url: str) -> str | None:
    """Return the 11-character YouTube video id from a URL, or None if not a YouTube URL."""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"(?:shorts\/)([0-9A-Za-z_-]{11})",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"^([0-9A-Za-z_-]{11})$",
    ]
    candidate = url.strip()
    if not candidate:
        return None
    for pattern in patterns:
        match = re.search(pattern, candidate)
        if match:
            return match.group(1)
    return None


def _sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", value).strip().rstrip(".")
    return (cleaned or "video")[:120]


def _proxy_url() -> str | None:
    """Return a single proxy URL (Webshare residential or generic) or None.

    Webshare credentials are expanded into the standard residential endpoint so
    the same proxy can be reused by both the transcript client and yt-dlp when
    downloading the video -- otherwise the video fetch would still hit YouTube
    from the blocked IP and fail with a captcha/"reload" error.
    """
    webshare_user, webshare_pass, generic_url = youtube_proxy_settings()
    if generic_url:
        return generic_url
    if webshare_user and webshare_pass:
        return f"http://{webshare_user}:{webshare_pass}@res.webshare.io:80/"
    return None


def _base_options(target_dir: Path) -> dict:
    """yt-dlp options shared across attempts: output, proxy and quiet flags."""
    options = {
        "outtmpl": str(target_dir / "youtube.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    proxy = _proxy_url()
    if proxy:
        options["proxy"] = proxy
    return options


def _subtitle_options(outdir: Path) -> dict:
    """yt-dlp options to fetch on-YouTube subtitles only (no video download)."""
    options = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": PREFERRED_LANGUAGES,
        "subtitlesformat": "srt",
        "outtmpl": str(outdir / "sub.%(ext)s"),
    }
    proxy = _proxy_url()
    if proxy:
        options["proxy"] = proxy
    return options


def _download_attempts(target_dir: Path) -> list[dict]:
    """Candidate yt-dlp option sets, tried in order until one downloads a file.

    A single client can lack a matching format (e.g. Android without audio-only
    ``ba`` makes ``bv*+ba`` fail with "Requested format is not available"), so we
    fall back across clients and looser format selectors.
    """
    base = _base_options(target_dir)
    return [
        # Prefer separate best video + audio (merges), Android avoids the captcha.
        {**base, "format": "bv*+ba/b", "extractor_args": {"youtube": {"player_client": ["android"]}}},
        # iOS exposes a wider format set (incl. audio-only) without the captcha.
        {**base, "format": "bv*+ba/b", "extractor_args": {"youtube": {"player_client": ["ios"]}}},
        # TV client serves combined high-quality mp4 (video+audio together).
        {**base, "format": "best", "extractor_args": {"youtube": {"player_client": ["tv"]}}},
        # Last resort: any client, any format that includes audio.
        {**base, "format": "bestvideo+bestaudio/best"},
    ]


def download_video(url: str, target_dir: Path) -> Path:
    """Download the video as MP4 into ``target_dir`` and return its path."""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง yt-dlp") from exc

    last_error: Exception | None = None
    for options in _download_attempts(target_dir):
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=True)
            candidates = list(target_dir.glob("youtube.*"))
            if candidates:
                target = candidates[0]
                if not target.is_file():
                    # yt-dlp sometimes keeps the original extension after merging.
                    continue
                ext = "mp4" if info.get("ext") == "mp4" else (info.get("ext") or "mp4")
                intended = target_dir / f"youtube.{ext}"
                if intended.exists() and intended != target:
                    target = intended
                return target
        except Exception as exc:
            last_error = exc
            # A captcha/reload or format error on one client → try the next.
            for stale in target_dir.glob("youtube.*"):
                with suppress(OSError):
                    stale.unlink()
            continue
    raise YouTubeError(f"ดาวน์โหลดวิดีโอจาก YouTube ไม่สำเร็จ: {last_error}")


def _attempt_subtitle(outdir: Path, url: str, language: str) -> str | None:
    """Try to fetch ``language`` subtitles with yt-dlp; return SRT text or None.

    Each language is fetched on its own so a rate-limit on a later language does
    not discard an already-fetched one (seen as HTTP 429 when requesting
    ``['th', 'en']`` together).
    """
    from yt_dlp import YoutubeDL

    options = {**_subtitle_options(outdir), "subtitleslangs": [language]}
    for stale in outdir.glob("sub.*"):
        with suppress(OSError):
            stale.unlink()
    with YoutubeDL(options) as downloader:
        downloader.download([url])
    candidates = sorted(outdir.glob(f"sub.{language}.srt"))
    if not candidates:
        return None
    text = candidates[0].read_text(encoding="utf-8", errors="replace").strip()
    return text or None


def fetch_subtitle(url: str) -> tuple[str, str]:
    """Fetch an available subtitle for ``url`` as SRT text plus its language code.

    Uses yt-dlp (the same tool that downloads the video) because it reliably
    lists and fetches on-YouTube captions through the configured proxy, unlike
    youtube-transcript-api which can fail with a blank response ("no element
    found") on some videos.  Thai is preferred, then English, then the first
    available track.  Each language is requested separately to avoid dropping an
    already-fetched track to a 429 on a later one.
    """
    if not extract_video_id(url):
        raise YouTubeError("ลิงก์ YouTube ไม่ถูกต้อง")
    try:
        import yt_dlp  # noqa: F401  (ensures the dependency is importable)
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง yt-dlp") from exc

    outdir = Path(tempfile.mkdtemp(prefix="sub-"))
    try:
        for language in PREFERRED_LANGUAGES:
            try:
                text = _attempt_subtitle(outdir, url, language)
            except Exception as exc:
                message = str(exc)
                if "IpBlocked" in message or "TooManyRequests" in message or "reload" in message:
                    raise YouTubeError("YouTube บล็อก IP/จำกัดการเรียกชั่วคราว ลองอีกครั้งภายหลัง") from exc
                continue
            if text:
                return text, language

        # Fallback: discover which tracks exist, then fetch the first available.
        from yt_dlp import YoutubeDL

        options = {**_subtitle_options(outdir), "writesubtitles": False, "writeautomaticsub": False}
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        auto = info.get("automatic_captions") or {}
        manual = info.get("subtitles") or {}
        available = list(auto) or list(manual)
        if not available:
            raise YouTubeError("วิดีโอนี้ไม่มีคำบรรยายที่นำเข้าพากย์ได้")
        text = _attempt_subtitle(outdir, url, available[0])
        if not text:
            raise YouTubeError("คำบรรยายที่ดึงได้ว่างเปล่า")
        return text, available[0]
    finally:
        for stale in outdir.glob("sub.*"):
            with suppress(OSError):
                stale.unlink()
        with suppress(OSError):
            outdir.rmdir()