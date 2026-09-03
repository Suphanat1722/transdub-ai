"""Download a YouTube video and fetch its on-Youtube subtitles.

The pipeline entry point is a YouTube URL: the actual video file (needed for
Demucs separation and muxing) is downloaded with yt-dlp, and the subtitles are
read straight from YouTube with youtube-transcript-api.  Thai subtitles become
the dub text directly; other languages are sent to Gemini for translation.

Neither call needs a YouTube API key.
"""

from __future__ import annotations

import re
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


def _build_transcript_api():
    """Build a YouTubeTranscriptApi, routing through a residential proxy when configured.

    Mirrors the reference extractor's approach: Webshare residential credentials
    take priority over a generic proxy URL; with neither set it connects directly.
    """
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    webshare_user, webshare_pass, generic_url = youtube_proxy_settings()
    if webshare_user and webshare_pass:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=webshare_user, proxy_password=webshare_pass
            )
        )
    if generic_url:
        return YouTubeTranscriptApi(
            proxy_config=GenericProxyConfig(http_url=generic_url, https_url=generic_url)
        )
    return YouTubeTranscriptApi()


def _download_options(target_dir: Path) -> dict:
    """yt-dlp options, adding a proxy URL when configured."""
    _, _, generic_url = youtube_proxy_settings()
    options = {
        "format": "bv*+ba/b",
        "outtmpl": str(target_dir / "youtube.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if generic_url:
        options["proxy"] = generic_url
    return options


def download_video(url: str, target_dir: Path) -> Path:
    """Download the video as MP4 into ``target_dir`` and return its path."""
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง yt-dlp") from exc

    try:
        with YoutubeDL(_download_options(target_dir)) as downloader:
            info = downloader.extract_info(url, download=True)
        ext = "mp4" if info.get("ext") == "mp4" else (info.get("ext") or "mp4")
    except Exception as exc:
        raise YouTubeError(f"ดาวน์โหลดวิดีโอจาก YouTube ไม่สำเร็จ: {exc}") from exc

    target = target_dir / f"youtube.{ext}"
    if not target.is_file():
        # yt-dlp sometimes keeps the original extension even when merging to mp4.
        candidates = list(target_dir.glob("youtube.*"))
        if candidates:
            target = candidates[0]
        else:
            raise YouTubeError("ดาวน์โหลดวิดีโอแล้วแต่ไม่พบไฟล์ผลลัพธ์")
    return target


def fetch_subtitle(url: str) -> tuple[str, str]:
    """Fetch an available subtitle for ``url`` as SRT text plus its language code.

    Thai is preferred, then English, then the first available track.  Raises
    ``YouTubeError`` when the video has no captions or YouTube blocks the request.
    """
    video_id = extract_video_id(url)
    if not video_id:
        raise YouTubeError("ลิงก์ YouTube ไม่ถูกต้อง")
    try:
        from youtube_transcript_api.formatters import SRTFormatter
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง youtube-transcript-api") from exc

    transcript_api = _build_transcript_api()
    try:
        transcript_list = transcript_api.list(video_id)
    except Exception as exc:
        raise YouTubeError(f"อ่านรายการคำบรรยายจาก YouTube ไม่สำเร็จ: {exc}") from exc

    transcript = None
    for language in PREFERRED_LANGUAGES:
        try:
            transcript = transcript_list.find_transcript([language])
            break
        except Exception:
            continue
    if transcript is None:
        try:
            transcript = next(iter(transcript_list))
        except Exception as exc:
            raise YouTubeError("วิดีโอนี้ไม่มีคำบรรยายที่นำเข้าพากย์ได้") from exc

    try:
        data = transcript.fetch()
        srt_content = SRTFormatter().format_transcript(data)
    except Exception as exc:
        message = str(exc)
        if "IpBlocked" in message or "TooManyRequests" in message:
            raise YouTubeError("YouTube บล็อก IP/จำกัดการเรียกชั่วคราว ลองอีกครั้งภายหลัง") from exc
        raise YouTubeError(f"ดึงคำบรรยายไม่สำเร็จ: {message}") from exc
    if not srt_content.strip():
        raise YouTubeError("คำบรรยายที่ดึงได้ว่างเปล่า")
    return srt_content, transcript.language_code