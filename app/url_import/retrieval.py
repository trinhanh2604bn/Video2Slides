"""Bounded media retrieval: yt-dlp (opt-in) and the Facebook Graph API source.

Safety properties:
* yt-dlp is used as a Python library (no shell), restricted to the YouTube /
  Facebook single-video extractors, no playlists, no cookies, no config files.
* Every HTTP request yt-dlp makes goes through ``GuardedYDL.urlopen``, which
  enforces https + platform/CDN host allowlist + public-IP DNS, and re-checks
  the final URL after redirects.
* Low resolution (<= URL_MAX_HEIGHT) is preferred; byte count, duration and a
  wall-clock deadline are enforced while downloading.
* Output goes to a fixed name inside a per-job temp directory, so titles never
  become file names (no path traversal).
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import settings
from .errors import UrlImportError
from .urls import MEDIA_SUFFIXES, ParsedUrl, check_outbound

log = logging.getLogger(__name__)

EXTRACTORS = {"youtube": ["youtube"], "facebook": ["facebook", "facebook:reel"]}


class _Abort(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Ordered: first match wins. Patterns are matched against the lower-cased error text.
_ERROR_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (("private video", "this video is private", "content isn't available", "content is not available"), "private"),
    (("confirm your age", "age-restricted", "age restricted", "inappropriate for some users"), "age_restricted"),
    (("not a bot", "confirm you", "captcha"), "bot_check"),
    (("sign in", "log in", "login", "logged-in", "cookies", "registered users", "members-only", "join this channel"),
     "login_required"),
    (("not available in your country", "geo restrict", "geo-restrict", "region", "your location"), "geo_restricted"),
    (("live event", "is live", "premiere", "is_live", "live stream", "will begin"), "live_not_supported"),
    (("drm",), "drm"),
    (("429", "too many requests", "rate limit", "rate-limit"), "rate_limited"),
    (("timed out", "timeout", "read operation timed out"), "timeout"),
    (("video unavailable", "has been removed", "does not exist", "not available", "404", "no video formats"),
     "unavailable"),
    (("unsupported url", "no suitable extractor"), "unsupported_url"),
    (("requested format is not available",), "extraction_failed"),
    (("unable to download", "connection", "network", "name resolution", "getaddrinfo"), "network_error"),
]


def map_error(exc: BaseException, default: str = "extraction_failed") -> UrlImportError:
    """Turn any yt-dlp/httpx exception into a user-safe UrlImportError."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, UrlImportError):
            return cur
        if isinstance(cur, _Abort):
            return UrlImportError(cur.code)
        if isinstance(cur, httpx.TimeoutException):
            return UrlImportError("timeout")
        exc_info = getattr(cur, "exc_info", None)
        nested = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) > 1 else None
        cur = nested or cur.__cause__ or cur.__context__
    text = str(exc).lower()
    for needles, code in _ERROR_PATTERNS:
        if any(n in text for n in needles):
            return UrlImportError(code)
    return UrlImportError(default)


def _make_ydl_class():
    from yt_dlp import YoutubeDL

    class GuardedYDL(YoutubeDL):
        allowed_suffixes: tuple[str, ...] = ()

        def urlopen(self, req):
            url = req if isinstance(req, str) else getattr(req, "url", None) or req.get_full_url()
            check_outbound(url, self.allowed_suffixes)
            resp = super().urlopen(req)
            final = getattr(resp, "url", None)
            if final and final != url:
                check_outbound(final, self.allowed_suffixes)
            return resp

    return GuardedYDL


class _QuietLogger:
    """yt-dlp messages may contain signed media URLs; only keep short summaries."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.info("yt-dlp warning: %s", str(msg)[:160])

    def error(self, msg):
        log.info("yt-dlp error: %s", str(msg)[:160])


def _base_opts(platform: str) -> dict[str, Any]:
    h = settings.url_max_height
    return {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "playlist_items": "1",
        "allowed_extractors": EXTRACTORS[platform],
        "socket_timeout": settings.download_socket_timeout_sec,
        "retries": settings.download_retries,
        "fragment_retries": settings.download_retries,
        "extractor_retries": min(settings.download_retries, 1),
        "file_access_retries": 1,
        "max_filesize": settings.url_max_download_bytes,
        "cachedir": False,
        "cookiefile": None,
        "check_formats": False,
        "lazy_playlist": True,
        # Plain https only (no HLS/DASH manifests fetched by ffmpeg), lowest sensible resolution.
        "format": (f"bv*[height<={h}][protocol=https]+ba[protocol=https]/"
                   f"b[height<={h}][protocol=https]/"
                   f"wv*[protocol=https]+ba[protocol=https]/w[protocol=https]"),
        "format_sort": [f"res:{h}", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": True,
    }


def _check_info(info: dict[str, Any]) -> None:
    if not isinstance(info, dict):
        raise UrlImportError("extraction_failed")
    if info.get("_type") in ("playlist", "multi_video"):
        raise UrlImportError("unsupported_url")
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        raise UrlImportError("live_not_supported")
    if info.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
        raise UrlImportError("login_required" if info["availability"] != "private" else "private")
    if info.get("has_drm") or any(f.get("has_drm") for f in info.get("requested_formats") or []):
        raise UrlImportError("drm")
    check_duration(info.get("duration"))
    est = estimate_size(info)
    if est and est > settings.url_max_download_bytes:
        raise UrlImportError("too_large", f"Ước tính {est / 1048576:.0f} MB, giới hạn {settings.url_max_download_mb} MB.")


def check_duration(duration: Any) -> None:
    try:
        d = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        d = None
    if d is not None and d > settings.url_max_duration_sec:
        raise UrlImportError(
            "too_long", f"Video dài {d / 60:.1f} phút, giới hạn {settings.url_max_duration_sec / 60:.0f} phút.")


def estimate_size(info: dict[str, Any]) -> int | None:
    formats = info.get("requested_formats") or [info]
    total = 0
    for f in formats:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and info.get("duration"):
            size = float(f["tbr"]) * 1000 / 8 * float(info["duration"])
        if not size:
            return None
        total += int(size)
    return total


def probe_ytdlp(parsed: ParsedUrl) -> dict[str, Any]:
    """Extract (without downloading) and apply all pre-download checks."""
    ydl_cls = _make_ydl_class()
    ydl_cls.allowed_suffixes = MEDIA_SUFFIXES[parsed.platform]
    try:
        with ydl_cls(_base_opts(parsed.platform)) as ydl:
            info = ydl.extract_info(parsed.canonical_url, download=False)
            info = ydl.sanitize_info(info)
    except Exception as exc:  # yt-dlp raises many types; all mapped to user-safe codes
        raise map_error(exc) from None
    _check_info(info)
    return info


def download_ytdlp(parsed: ParsedUrl, dest: Path, work_dir: Path) -> dict[str, Any]:
    """Download one low-resolution MP4 to ``dest``; ``work_dir`` is always removed."""
    deadline = time.monotonic() + settings.download_timeout_sec
    sizes: dict[str, int] = {}

    def hook(d: dict[str, Any]) -> None:
        if time.monotonic() > deadline:
            raise _Abort("timeout")
        sizes[str(d.get("filename") or d.get("tmpfilename") or "")] = int(d.get("downloaded_bytes") or 0)
        if sum(sizes.values()) > settings.url_max_download_bytes:
            raise _Abort("too_large")

    ydl_cls = _make_ydl_class()
    ydl_cls.allowed_suffixes = MEDIA_SUFFIXES[parsed.platform]
    opts = _base_opts(parsed.platform) | {
        "outtmpl": {"default": "media.%(ext)s"},
        "paths": {"home": str(work_dir), "temp": str(work_dir)},
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        with ydl_cls(opts) as ydl:
            info = ydl.extract_info(parsed.canonical_url, download=False)
            _check_info(ydl.sanitize_info(info))
            ydl.process_ie_result(info, download=True)
        produced = sorted(work_dir.glob("media.mp4")) or sorted(work_dir.glob("media.*"))
        if not produced:
            raise UrlImportError("download_failed")
        if produced[0].stat().st_size > settings.url_max_download_bytes:
            raise UrlImportError("too_large")
        produced[0].replace(dest)
        return {"title": info.get("title"), "duration": info.get("duration")}
    except UrlImportError:
        raise
    except Exception as exc:
        raise map_error(exc, default="download_failed") from None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def download_http(source_url: str, dest: Path, platform: str, client: httpx.Client) -> None:
    """Stream an official media URL (e.g. Graph API ``source``) with byte and time limits."""
    deadline = time.monotonic() + settings.download_timeout_sec
    url = source_url
    allowed = MEDIA_SUFFIXES[platform]
    tmp = dest.with_suffix(".part")
    try:
        for _ in range(5):
            check_outbound(url, allowed)
            with client.stream("GET", url, follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    url = str(resp.url.join(resp.headers.get("location", "")))
                    continue
                if resp.status_code == 429:
                    raise UrlImportError("rate_limited")
                if resp.status_code in (401, 403):
                    raise UrlImportError("not_permitted")
                if resp.status_code != 200:
                    raise UrlImportError("download_failed")
                length = resp.headers.get("content-length")
                if length and length.isdigit() and int(length) > settings.url_max_download_bytes:
                    raise UrlImportError("too_large")
                written = 0
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(1024 * 256):
                        written += len(chunk)
                        if written > settings.url_max_download_bytes:
                            raise UrlImportError("too_large")
                        if time.monotonic() > deadline:
                            raise UrlImportError("timeout")
                        fh.write(chunk)
                if written == 0:
                    raise UrlImportError("download_failed")
                tmp.replace(dest)
                return
        raise UrlImportError("too_many_redirects")
    except UrlImportError:
        raise
    except httpx.TimeoutException:
        raise UrlImportError("timeout") from None
    except httpx.HTTPError:
        raise UrlImportError("network_error") from None
    finally:
        tmp.unlink(missing_ok=True)

