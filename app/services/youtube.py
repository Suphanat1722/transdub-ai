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
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ..core.config import youtube_proxy_settings

# Language chosen directly as the final dub text (no Gemini translation).
THAI_LANGUAGE = "th"
# Last-resort fallback order when the video's original language cannot be
# determined (English is the common original; Thai avoids a pointless Gemini
# translation when no other track exists).  An explicit source_language and any
# detected original language are always tried before these.
FALLBACK_LANGUAGES = ("en", THAI_LANGUAGE)

# Every yt-dlp request presents a real Chrome TLS fingerprint so a blocked IP is
# less likely to trip the "Sign in to confirm you're not a bot" check.  Building
# the target is lazy because it imports from yt-dlp, which is optional at module
# load time (e.g. running docs/tests without the network stack).
_IMPERSONATE = None


def _impersonate_target() -> object:
    """Return the yt-dlp ImpersonateTarget for Chrome, or None if unavailable.

    yt-dlp requires this to be an ``ImpersonateTarget`` object (a bare string
    trips an internal type assertion), and it only applies when the curl_cffi
    backend is installed -- which also powers the TLS impersonation.
    """
    global _IMPERSONATE
    if _IMPERSONATE is None:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            _IMPERSONATE = ImpersonateTarget.from_str("chrome")
        except Exception:
            _IMPERSONATE = False
    return _IMPERSONATE or None


class YouTubeError(RuntimeError):
    """An error downloading a video or fetching subtitles, safe to show in the UI."""


def _is_blocked_error(message: str) -> bool:
    """Return True when a yt-dlp error means YouTube blocked/rate-limited the request.

    These are transient (blocked IP, temporary throttling, or the "Sign in to
    confirm you're not a bot" captcha), so the caller can retry or tell the user
    to wait / use a residential proxy, rather than treating it as a permanent
    subtitle defect.
    """
    return any(
        keyword in message
        for keyword in (
            "IpBlocked",
            "TooManyRequests",
            "reload",
            "Sign in to confirm",
            "confirm you",
        )
    )


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
    """yt-dlp options shared across attempts: output, proxy, impersonation and quiet."""
    options = {
        "outtmpl": str(target_dir / "youtube.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    # Present a real Chrome TLS fingerprint (via curl_cffi) so a blocked IP
    # is less likely to hit the "Sign in to confirm you're not a bot" check.
    # Must be an ImpersonateTarget object: a bare string trips yt-dlp's
    # internal type assertion at YoutubeDL init.
    target = _impersonate_target()
    if target is not None:
        options["impersonate"] = target
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
        "subtitleslangs": FALLBACK_LANGUAGES,
        "subtitlesformat": "srt",
        "outtmpl": str(outdir / "sub.%(ext)s"),
    }
    # Mirror the download request's Chrome fingerprint so the same IP is not
    # singled out as a bot when it also reads the captions.
    target = _impersonate_target()
    if target is not None:
        options["impersonate"] = target
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


def download_video(
    url: str, target_dir: Path, progress: Callable[[float], None] | None = None
) -> Path:
    """Download the video as MP4 into ``target_dir`` and return its path.

    ``progress`` is an optional callable ``(percent: float)`` invoked as the
    download advances, letting the caller surface a progress bar.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง yt-dlp") from exc

    def hook(data: dict) -> None:
        if progress is None or data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        if total:
            progress(min(99.0, downloaded / total * 100.0))

    last_error: Exception | None = None
    for options in _download_attempts(target_dir):
        try:
            options["progress_hooks"] = [hook]
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


def _subtitle_attempt_options(outdir: Path, language: str) -> list[dict]:
    """Candidate yt-dlp option sets for fetching one subtitle language, tried in
    order.  Earlier player clients (android/ios/tv) dodge the "Sign in to confirm
    you're not a bot" captcha that some IPs get on the default client; the last
    entry is the unpinned default as a fallback.
    """
    base = _subtitle_options(outdir)
    base["subtitleslangs"] = [language]
    return [
        {**base, "extractor_args": {"youtube": {"player_client": ["android"]}}},
        {**base, "extractor_args": {"youtube": {"player_client": ["ios"]}}},
        {**base, "extractor_args": {"youtube": {"player_client": ["tv"]}}},
        base,
    ]


def _attempt_subtitle(outdir: Path, url: str, language: str) -> str | None:
    """Try to fetch ``language`` subtitles with yt-dlp; return SRT text or None.

    Each language is fetched on its own so a rate-limit on a later language does
    not discard an already-fetched one (seen as HTTP 429 when requesting
    ``['th', 'en']`` together).  Within a language, the player clients are tried
    in order so a blocked client falls through to another one.  If every client
    fails, the last error is raised so the caller can classify it (block / 429
    vs. other), instead of a generic None.
    """
    from yt_dlp import YoutubeDL

    last_error: Exception | None = None
    for options in _subtitle_attempt_options(outdir, language):
        for stale in outdir.glob("sub.*"):
            with suppress(OSError):
                stale.unlink()
        try:
            with YoutubeDL(options) as downloader:
                downloader.download([url])
        except Exception as exc:
            last_error = exc
            continue
        candidates = sorted(outdir.glob(f"sub.{language}.srt"))
        if not candidates:
            continue
        text = candidates[0].read_text(encoding="utf-8", errors="replace").strip()
        if text:
            return text
    if last_error:
        raise last_error
    return None


def _detect_subtitle_languages(info: dict, source_language: str = "auto") -> tuple[str, ...]:
    """Return subtitle language codes to try, best match first.

    Order: the user-selected ``source_language`` if non-auto, then the video's
    detected original language, then the fallbacks. ``info`` is a yt-dlp info
    dict; ``automatic_captions`` and ``subtitles`` map language code -> tracks.
    """
    available = [code for code in set(info.get("automatic_captions") or {}) | set(info.get("subtitles") or {})]
    if not available:
        return FALLBACK_LANGUAGES

    available_upper = {code.split("-")[0].upper(): code for code in available}

    def exact_or_family(requested: str) -> str | None:
        requested = requested.strip()
        if not requested or requested.lower() == "auto":
            return None
        if requested in available:
            return requested
        base = requested.split("-")[0].upper()
        return available_upper.get(base)

    # The video's original spoken-language tags and the configured source language.
    original = info.get("original_language") or info.get("language") or None
    wanted: list[str] = []
    if source_language and source_language.lower() != "auto":
        wanted.append(source_language)
    if original and original.lower() != "auto":
        wanted.append(original)

    ordered: list[str] = []
    for code in wanted:
        match = exact_or_family(code)
        if match and match not in ordered:
            ordered.append(match)
    for code in FALLBACK_LANGUAGES:
        match = exact_or_family(code)
        if match and match not in ordered:
            ordered.append(match)
    # Any remaining available track as a last resort (prefer manual/uploader subs).
    manual = set(info.get("subtitles") or {})
    for code in available:
        if code not in ordered and (code in manual or not ordered):
            ordered.append(code)
    return tuple(ordered)


def _available_languages(url: str) -> dict:
    """Return ``{"automatic_captions": {}, "subtitles": {}}`` for a video, or {} if unknown.

    ``original_language``, ``language``, ``automatic_captions`` and ``subtitles``
    let us pick the video's original language instead of defaulting to Thai.  Any
    failure here is non-fatal: the caller falls back to FALLBACK_LANGUAGES.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return {}
    options = {**_subtitle_options(Path(tempfile.mkdtemp(prefix="sub-")))}
    options.pop("writesubtitles", None)
    options.pop("writeautomaticsub", None)
    options["skip_download"] = True
    for client in ("android", "ios"):
        try:
            with YoutubeDL({**options, "extractor_args": {"youtube": {"player_client": [client]}}}) as downloader:
                return downloader.extract_info(url, download=False) or {}
        except Exception:
            continue
    return {}


def fetch_subtitle(url: str, source_language: str = "auto") -> tuple[str, str]:
    """Fetch an available subtitle for ``url`` as SRT text plus its language code.

    Uses yt-dlp (the same tool that downloads the video) because it reliably
    lists and fetches on-YouTube captions through the configured proxy, unlike
    youtube-transcript-api which can fail with a blank response ("no element
    found") on some videos.

    The language is chosen to match the video's original language rather than
    Thai: the user's ``source_language`` (if set) wins, then the original
    language detected from the video, then English, then Thai, then any track.
    Each language is requested separately to avoid dropping an already-fetched
    track to a 429 on a later one.
    """
    if not extract_video_id(url):
        raise YouTubeError("ลิงก์ YouTube ไม่ถูกต้อง")
    try:
        import yt_dlp  # noqa: F401  (ensures the dependency is importable)
    except ImportError as exc:
        raise YouTubeError("ยังไม่ได้ติดตั้ง yt-dlp") from exc

    outdir = Path(tempfile.mkdtemp(prefix="sub-"))
    try:
        languages = _detect_subtitle_languages(_available_languages(url), source_language)
        if not languages:
            languages = FALLBACK_LANGUAGES
        for language in languages:
            try:
                text = _attempt_subtitle(outdir, url, language)
            except Exception as exc:
                if _is_blocked_error(str(exc)):
                    raise YouTubeError("YouTube บล็อก IP/จํากัดการเรียกชั่วคราว ลองอีกครั้งภายหลัง") from exc
                continue
            if text:
                return text, language

# Fallback: discover which tracks exist, then fetch the first available.
        try:
            from yt_dlp import YoutubeDL

            options = {**_subtitle_options(outdir), "writesubtitles": False, "writeautomaticsub": False}
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception as exc:
            if _is_blocked_error(str(exc)):
                raise YouTubeError("YouTube บล็อก IP/จํากัดการเรียกชั่วคราว ลองอีกครั้งภายหลัง") from exc
            auto: dict = {}
            manual: dict = {}
        else:
            auto = info.get("automatic_captions") or {}
            manual = info.get("subtitles") or {}
        available = list(auto) or list(manual)
        if not available:
            raise YouTubeError("วิดีโอนี้ไม่มีคําบรรยายที่นําเข้าพากย์ได้")
        try:
            text = _attempt_subtitle(outdir, url, available[0])
        except Exception as exc:
            if _is_blocked_error(str(exc)):
                raise YouTubeError("YouTube บล็อก IP/จํากัดการเรียกชั่วคราว ลองอีกครั้งภายหลัง") from exc
            raise
        if not text:
            raise YouTubeError("คําบรรยายที่ดึงได้ว่างเปล่า")
        return text, available[0]
    finally:
        for stale in outdir.glob("sub.*"):
            with suppress(OSError):
                stale.unlink()
        with suppress(OSError):
            outdir.rmdir()