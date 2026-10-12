"""URL import orchestration: validate -> policy decision -> bounded download -> existing pipeline.

Downloads run in a small thread pool (``MAX_CONCURRENT_DOWNLOADS``, default 1),
separate from the single processing worker in :mod:`app.jobs`, so a download
can overlap with Whisper on another job but never more than N downloads run.
After a successful download the job joins the normal processing queue and goes
through exactly the same FFmpeg -> Whisper -> AI -> PPTX pipeline as uploads.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import jobs
from ..config import settings
from ..video import MediaError, validate
from . import metadata as md_mod
from . import retrieval
from .errors import UPLOAD_HINT, UrlImportError
from .urls import ParsedUrl, parse_video_url, resolve_short_link, safe_thumbnail

log = logging.getLogger(__name__)

PLATFORM_LABELS = {"youtube": "YouTube", "facebook": "Facebook"}
CACHE_TTL_SEC = 600
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
USER_AGENT = "Video2Slides/0.1 (+metadata check)"


@dataclass
class VideoCheck:
    platform: str
    platform_label: str
    url: str
    original_url: str
    video_id: str | None
    title: str | None
    author: str | None
    duration_seconds: float | None
    thumbnail: str | None
    status: str                 # ready | metadata_only | restricted | too_long | too_large | unavailable | error
    retrieval_permitted: bool
    retrieval_method: str | None
    message: str
    error_code: str | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


def http_client() -> httpx.Client:
    # trust_env=False: never route through proxies from the environment.
    return httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, trust_env=False)


def retrieval_mode(platform: str) -> str:
    mode = settings.youtube_retrieval if platform == "youtube" else settings.facebook_retrieval
    allowed = {"youtube": {"off", "ytdlp"}, "facebook": {"off", "graph_api", "ytdlp"}}[platform]
    return mode if mode in allowed else "off"


# ---------------------------------------------------------------------- validation

_cache: dict[str, tuple[float, VideoCheck, ParsedUrl]] = {}
_cache_lock = threading.Lock()

STATUS_FOR_CODE = {
    "too_long": "too_long", "too_large": "too_large", "unavailable": "unavailable",
    "private": "restricted", "login_required": "restricted", "age_restricted": "restricted",
    "geo_restricted": "restricted", "live_not_supported": "restricted", "drm": "restricted",
    "bot_check": "restricted", "not_permitted": "metadata_only", "retrieval_disabled": "metadata_only",
}


def check_url(raw: str, client_factory: Callable[[], httpx.Client] = http_client) -> tuple[VideoCheck, ParsedUrl]:
    """Validate a URL and decide whether retrieval is permitted.

    Raises UrlImportError only for invalid/unsupported URLs; access problems are
    returned as a VideoCheck with ``retrieval_permitted=False`` so the UI can
    still show what it knows and offer the upload fallback.
    """
    if not settings.url_import_enabled:
        raise UrlImportError("disabled")
    parsed = parse_video_url(raw)  # raises invalid_url / unsupported_*

    with _cache_lock:
        hit = _cache.get(parsed.canonical_url) if not parsed.needs_resolve else None
        if hit and time.time() - hit[0] < CACHE_TTL_SEC:
            return hit[1], hit[2]

    mode = retrieval_mode(parsed.platform)
    md = md_mod.Metadata()
    code: str | None = None
    method: str | None = None
    try:
        with client_factory() as client:
            if parsed.needs_resolve:
                parsed = resolve_short_link(parsed, client)
            if parsed.platform == "youtube":
                md = md_mod.youtube_metadata(parsed, client)
            else:
                md = md_mod.facebook_metadata(parsed, client, want_source=(mode == "graph_api"))
        if md.is_live:
            raise UrlImportError("live_not_supported")
        retrieval.check_duration(md.duration_seconds)

        if mode == "off":
            code = "retrieval_disabled"
        elif mode == "graph_api":
            if md.graph_source_readable:
                method = "graph_api"
            else:
                code = "not_permitted"
        else:  # ytdlp, explicitly enabled by the operator
            info = retrieval.probe_ytdlp(parsed)
            md.title = md.title or (str(info.get("title") or "")[:300] or None)
            md.author = md.author or (str(info.get("uploader") or "")[:150] or None)
            if info.get("duration"):
                md.duration_seconds = float(info["duration"])
            md.thumbnail = md.thumbnail or _thumb(info)
            method = "ytdlp"
    except UrlImportError as exc:
        if exc.code in ("invalid_url", "unsupported_url", "unsupported_platform", "blocked_address",
                        "too_many_redirects"):
            raise
        code = exc.code
        method = None
        err_detail = exc.detail
    except httpx.TimeoutException:
        code, method, err_detail = "timeout", None, None
    except httpx.HTTPError:
        code, method, err_detail = "network_error", None, None
    else:
        err_detail = None

    if code is None:
        status, message = "ready", "Video sẵn sàng để tạo bài trình chiếu."
    else:
        status = STATUS_FOR_CODE.get(code, "error")
        message = f"{UrlImportError(code, err_detail).message} {UPLOAD_HINT}"
    check = VideoCheck(
        platform=parsed.platform,
        platform_label=PLATFORM_LABELS[parsed.platform],
        url=parsed.canonical_url,
        original_url=raw.strip()[:2048],
        video_id=parsed.video_id,
        title=md.title,
        author=md.author,
        duration_seconds=round(md.duration_seconds, 1) if md.duration_seconds else None,
        thumbnail=md.thumbnail,
        status=status,
        retrieval_permitted=code is None,
        retrieval_method=method if code is None else None,
        message=message,
        error_code=code,
    )
    # Transient failures are not cached so a retry re-checks.
    if code not in ("timeout", "network_error", "rate_limited"):
        with _cache_lock:
            now = time.time()
            for k in [k for k, v in _cache.items() if now - v[0] > CACHE_TTL_SEC]:
                _cache.pop(k, None)
            _cache[parsed.canonical_url] = (now, check, parsed)
    return check, parsed


def _thumb(info: dict[str, Any]) -> str | None:
    return safe_thumbnail(info.get("thumbnail"))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------- downloads

_executor: ThreadPoolExecutor | None = None
_pending = 0
_pending_lock = threading.Lock()
def pending_downloads() -> int:
    return _pending


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_downloads, thread_name_prefix="url-dl")
    return _executor


def submit_download(job_id: str) -> None:
    global _pending
    with _pending_lock:
        _pending += 1
    jobs.update(job_id, status="downloading", stage="downloading", error=None, finished_at=None)
    _get_executor().submit(_run_download, job_id)


def _default_fetch(source: dict[str, Any], d: Path) -> None:
    """Retrieve the media into ``d / input.mp4`` using the decided method."""
    from ..pipeline import INPUT_NAME

    parsed = ParsedUrl(source["platform"], source.get("kind", "video"), source.get("video_id"), source["url"])
    dest = d / INPUT_NAME
    if source["method"] == "graph_api":
        with http_client() as client:
            src = md_mod.graph_source_url(parsed.video_id or "", client)
            retrieval.download_http(src, dest, parsed.platform, client)
    elif source["method"] == "ytdlp":
        retrieval.download_ytdlp(parsed, dest, d / "download")
    else:
        raise UrlImportError("retrieval_disabled")


# Indirection so tests can replace the network part with a local file copy.
fetch_media: Callable[[dict[str, Any], Path], None] = _default_fetch


def _free_space_ok(path: Path) -> bool:
    try:
        return shutil.disk_usage(path).free > settings.url_max_download_bytes * 2 + 200 * 1024 * 1024
    except OSError:
        return True


def _run_download(job_id: str) -> None:
    global _pending
    from ..ai import recommend_slide_count
    from ..pipeline import INPUT_NAME

    try:
        d = jobs.job_dir(job_id)
        job = jobs.load(job_id)
        source = job.get("source") or {}
        # Re-check policy at execution time: the operator may have disabled retrieval meanwhile.
        if retrieval_mode(source.get("platform", "")) != source.get("method"):
            raise UrlImportError("retrieval_disabled")
        if not _free_space_ok(d):
            raise UrlImportError("insufficient_storage")
        jobs.update(job_id, started_at=time.time())
        jobs.set_stage(job_id, "downloading")
        fetch_media(source, d)

        target = d / INPUT_NAME
        try:
            info = validate(target)
        except MediaError as exc:
            target.unlink(missing_ok=True)
            raise UrlImportError("download_failed", str(exc)) from None
        if info.duration > settings.url_max_duration_sec:
            target.unlink(missing_ok=True)
            retrieval.check_duration(info.duration)

        options = dict(job.get("options") or {})
        if not options.get("slide_count"):
            options["recommended_slides"] = recommend_slide_count(info.duration, int(info.duration * 2.3))
        jobs.update(job_id, options=options, media={
            "duration": round(info.duration, 2), "width": info.width, "height": info.height,
            "video_codec": info.video_codec, "audio_codec": info.audio_codec, "size_bytes": info.size_bytes,
        })
        jobs.enqueue(job_id)
    except UrlImportError as exc:
        _fail(job_id, f"{exc.message} {UPLOAD_HINT}")
    except jobs.JobNotFound:
        pass  # deleted while waiting
    except Exception:
        log.exception("download for job %s crashed", job_id)
        _fail(job_id, f"Tải video thất bại do lỗi không mong muốn. {UPLOAD_HINT}")
    finally:
        with _pending_lock:
            _pending -= 1
        try:
            shutil.rmtree(jobs.job_dir(job_id) / "download", ignore_errors=True)
        except jobs.JobNotFound:
            pass


def _fail(job_id: str, message: str) -> None:
    try:
        jobs.update(job_id, status="failed", finished_at=time.time(), error=message)
    except jobs.JobNotFound:
        pass
