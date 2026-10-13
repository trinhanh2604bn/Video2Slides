"""FastAPI entry point: REST API + static single-page frontend."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import jobs, library, preview
from .ai import Presentation, active_provider_name, recommend_slide_count
from .ai.health import llm_status
from .config import SUPPORTED_LANGUAGES, settings
from .google_slides import GoogleError
from .google_slides import auth as google_auth
from .google_slides.export import check_sync, fetch_thumbnail
from .illustrations import usage as image_usage
from .illustrations.planner import DENSITIES
from .pipeline import INPUT_NAME, google_ready, load_outline, output_path, run_job, submit_google_export
from .pptx_engine import THEMES
from .pptx_engine.builder import LAYOUT_MODES
from .reading import slide_payload, speaker_notes_text
from .transcribe import Transcript
from .url_import import service as url_service
from .url_import.errors import UrlImportError
from .video import MediaError, basic_validate, ffmpeg_available, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs full request URLs at INFO; keep them out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("app")

STATIC_DIR = Path(__file__).parent / "static"
CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    jobs.start_worker(run_job)
    yield


app = FastAPI(title="Video2Slides", version="0.1.0", lifespan=lifespan)


def require_access(x_app_password: str | None = Header(default=None)) -> None:
    """Optional shared-password gate (enabled when APP_PASSWORD is set)."""
    if not settings.app_password:
        return
    if not x_app_password or not hmac.compare_digest(x_app_password.encode(), settings.app_password.encode()):
        raise HTTPException(status_code=401, detail="Sai hoặc thiếu mật khẩu truy cập.")


def _load_job(job_id: str) -> dict:
    try:
        return jobs.load(job_id)
    except jobs.JobNotFound:
        raise HTTPException(status_code=404, detail="Không tìm thấy công việc.") from None


def _public(job: dict) -> dict:
    """Job fields that are safe to return to the browser."""
    keys = ("id", "status", "stage", "stage_times", "created_at", "started_at", "finished_at",
            "filename", "options", "media", "error", "error_code", "result", "illustrations", "progress",
            "cancel_requested", "status_message")
    out = {k: job.get(k) for k in keys}
    out["google"] = _public_google(job.get("google"))
    src = job.get("source") or {"type": "upload"}
    out["source"] = {k: src.get(k) for k in ("type", "platform", "url", "title", "thumbnail") if k in src}
    if job.get("status") == "queued":
        out["queue_length"] = jobs.pending_count()
    return out


def _public_google(g: dict | None) -> dict | None:
    """Google export state without internal details (no slide id mapping, never any token)."""
    if not g:
        return None
    meta = g.get("meta") or {}
    return {
        "state": g.get("state"), "step": g.get("step"), "error": g.get("error"), "code": g.get("code"),
        "url": meta.get("url"), "title": meta.get("title"), "created_at": meta.get("created_at"),
        "notes": meta.get("notes"), "warnings": (meta.get("validation") or {}).get("warnings") or [],
        "slide_count": (meta.get("validation") or {}).get("slide_count"),
    }


def _safe_display_name(name: str | None) -> str:
    base = Path(name or "video.mp4").name
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE).strip() or "video.mp4"
    return base[:120]


def _llm_config() -> dict:
    name = active_provider_name()
    model = {"ollama": settings.ollama_model, "gemini": settings.gemini_model,
             "modal_ollama": settings.modal_llm_model}.get(name)
    out = {"provider": name, "model": model, "local": name in ("ollama", "offline")}
    if name == "auto":
        out.update(gemini_configured=bool(settings.gemini_api_key), gemini_model=settings.gemini_model,
                   fallback_model=settings.modal_llm_model, fallback_gpu=settings.modal_llm_gpu)
    return out


@app.get("/api/health")
def health() -> dict:
    tools = ffmpeg_available()
    if settings.media_on_modal:
        # FFmpeg and Whisper run on the Modal media worker; local FFmpeg is optional.
        return {"ok": True, "media_processing": "modal", "ffmpeg": tools["ffmpeg"], "ffprobe": tools["ffprobe"]}
    return {"ok": all(tools.values()), "media_processing": "local", "ffmpeg": tools["ffmpeg"],
            "ffprobe": tools["ffprobe"]}


@app.get("/api/llm/status", dependencies=[Depends(require_access)])
def llm_health() -> dict:
    """Is the configured LLM usable right now? (Ollama reachable, model installed; never a secret)."""
    return llm_status()


@app.get("/api/config")
def public_config() -> dict:
    return {
        "max_upload_mb": settings.max_upload_mb,
        "max_duration_sec": settings.max_duration_sec,
        "languages": SUPPORTED_LANGUAGES,
        "default_language": settings.default_language,
        "styles": {k: t.label for k, t in THEMES.items()},
        "default_style": "professional",
        "ai_provider": active_provider_name(),
        "llm": _llm_config(),
        "password_required": bool(settings.app_password),
        "whisper_model": settings.whisper_model,
        "media_processing": "modal" if settings.media_on_modal else "local",
        "preview_engine": preview.engine(),
        "ai_images": _ai_images_config(),
        "layout_modes": list(LAYOUT_MODES),
        "default_layout_mode": settings.default_layout_mode if settings.default_layout_mode in LAYOUT_MODES
        else "presentation",
        "google": google_auth.status(),
        "url_import": {
            "enabled": settings.url_import_enabled,
            "max_duration_sec": settings.url_max_duration_sec,
            "max_download_mb": settings.url_max_download_mb,
            "retrieval": {p: url_service.retrieval_mode(p) for p in ("youtube", "facebook")},
        },
    }


def _ai_images_config() -> dict:
    av = image_usage.availability()
    return {
        "enabled": settings.ai_images_enabled,
        "available": av.available,
        "reason": av.reason,
        "max_per_presentation": settings.image_max_per_presentation,
        "densities": list(DENSITIES),
        "placeholder": settings.image_provider == "mock",
    }


@app.post("/api/jobs", dependencies=[Depends(require_access)], status_code=202)
def create_job(
    file: UploadFile = File(...),
    title: str = Form(default="", max_length=150),
    language: str = Form(default=""),
    style: str = Form(default="professional"),
    slide_count: int = Form(default=0, ge=0, le=40),
    ai_images: bool = Form(default=False),
    image_density: str = Form(default="standard"),
    layout_mode: str = Form(default=""),
    google_slides: bool = Form(default=False),
) -> dict:
    """Upload an MP4 and queue it for processing. Runs in a worker thread (sync def)."""
    if _busy():
        raise HTTPException(status_code=429, detail="Hệ thống đang bận, vui lòng thử lại sau ít phút.")

    display = _safe_display_name(file.filename)
    _check_disk_space(getattr(file, "size", None) or 0)
    if not display.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ tệp .mp4.")
    options = _job_options(title, language, style, slide_count, ai_images, image_density, layout_mode,
                           google_slides)
    job_id, d = jobs.new_job(options, display)
    target = d / INPUT_NAME
    written = 0
    digest = hashlib.sha256()
    try:
        with target.open("wb") as fh:
            while chunk := file.file.read(CHUNK):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail=f"Tệp vượt quá {settings.max_upload_mb} MB.")
                digest.update(chunk)
                fh.write(chunk)
        content_hash = digest.hexdigest()
        if existing := jobs.find_active_duplicate(content_hash, options):
            # Double click / resubmit of a video that is already being processed: follow that job.
            jobs.delete(job_id)
            return {"job_id": existing, "queue_position": jobs.pending_count(), "duplicate": True}
        info = _validate_upload(target)
    except MediaError as exc:
        jobs.delete(job_id)
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except HTTPException:
        jobs.delete(job_id)
        raise
    except Exception:
        jobs.delete(job_id)
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="Không lưu được tệp tải lên.") from None
    finally:
        file.file.close()

    media = None
    if info is not None:
        if not options["slide_count"]:
            # Rough recommendation; the AI refines it from the transcript.
            options["recommended_slides"] = recommend_slide_count(info.duration, int(info.duration * 2.3))
        media = {"duration": round(info.duration, 2), "width": info.width, "height": info.height,
                 "video_codec": info.video_codec, "audio_codec": info.audio_codec, "size_bytes": info.size_bytes}
    # Without local FFmpeg (cloud processing) the media facts are filled in by the Modal media worker.
    jobs.update(job_id, options=options, content_hash=content_hash, media=media)
    position = jobs.enqueue(job_id)
    return {"job_id": job_id, "queue_position": position}


def _check_disk_space(incoming_bytes: int) -> None:
    """Long videos are large: refuse early instead of filling the disk (the job folder keeps the video)."""
    import shutil

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.data_dir).free
    if free - incoming_bytes < settings.min_free_disk_mb * 1024 * 1024:
        raise HTTPException(status_code=507, detail=(
            f"Ổ đĩa còn trống {free / 2**30:.1f} GB, không đủ để lưu video này "
            f"(cần giữ lại tối thiểu {settings.min_free_disk_mb / 1024:.0f} GB). Hãy giải phóng dung lượng."))


def _validate_upload(path: Path):
    """Full ffprobe validation when FFmpeg is installed here; otherwise (cloud processing) size + MP4
    signature now, and duration/streams on the Modal media worker before any GPU work."""
    if settings.media_on_modal and not all(ffmpeg_available().values()):
        from . import media_local

        if media_local.available():  # PyAV (bundled with faster-whisper): full checks without FFmpeg
            return media_local.validate(path)
        basic_validate(path)
        return None
    return validate(path)


def _busy() -> bool:
    return jobs.pending_count() + url_service.pending_downloads() >= settings.max_queue_size


def _job_options(title: str, language: str, style: str, slide_count: int, ai_images: bool = False,
                 image_density: str = "standard", layout_mode: str = "", google_slides: bool = False) -> dict:
    language = language or settings.default_language
    layout_mode = layout_mode or settings.default_layout_mode
    if layout_mode not in LAYOUT_MODES:
        raise HTTPException(status_code=400, detail="Kiểu bố cục slide không hợp lệ.")
    if google_slides and not google_ready():
        raise HTTPException(status_code=409, detail="Chưa kết nối tài khoản Google. Hãy bấm 'Kết nối Google' "
                                                    "hoặc bỏ chọn tùy chọn tạo Google Slides.")
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=400, detail="Ngôn ngữ không được hỗ trợ.")
    if style not in THEMES:
        raise HTTPException(status_code=400, detail="Phong cách không hợp lệ.")
    if image_density not in DENSITIES:
        raise HTTPException(status_code=400, detail="Mật độ ảnh minh họa không hợp lệ.")
    if ai_images:
        av = image_usage.availability()
        if not av.available:
            # Explicit refusal instead of silently producing a deck without the requested images.
            raise HTTPException(status_code=409, detail=f"{av.reason} Hãy tắt tùy chọn ảnh minh họa AI và thử lại.")
    return {"title": title.strip(), "language": language, "style": style, "slide_count": slide_count or None,
            "illustrations": {"enabled": bool(ai_images), "density": image_density},
            "layout_mode": layout_mode, "google_slides": bool(google_slides)}


# ---------------------------------------------------------------------- URL import

_rate_hits: dict[str, deque] = {}
_rate_lock = threading.Lock()


def url_rate_limit(request: Request) -> None:
    """Optional per-IP limit for the URL endpoints (URL_RATE_LIMIT_PER_MIN, 0 = off)."""
    limit = settings.url_rate_limit_per_min
    if limit <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    with _rate_lock:
        hits = _rate_hits.setdefault(ip, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= limit:
            raise HTTPException(status_code=429, detail="Bạn gửi quá nhiều yêu cầu, vui lòng thử lại sau một phút.")
        hits.append(now)
        if len(_rate_hits) > 10_000:
            _rate_hits.clear()


class UrlValidateIn(BaseModel):
    url: str = Field(max_length=2048)


class UrlImportIn(UrlValidateIn):
    rights_confirmed: bool = False
    title: str = Field(default="", max_length=150)
    language: str = ""
    style: str = "professional"
    slide_count: int = Field(default=0, ge=0, le=40)
    ai_images: bool = False
    image_density: str = "standard"
    layout_mode: str = ""
    google_slides: bool = False


def _url_error(exc: UrlImportError) -> HTTPException:
    return HTTPException(status_code=404 if exc.code == "disabled" else 400, detail=exc.message)


@app.post("/api/videos/url/validate", dependencies=[Depends(require_access), Depends(url_rate_limit)])
def validate_video_url(body: UrlValidateIn) -> dict:
    """Parse the URL, fetch permitted metadata and report whether retrieval is allowed."""
    try:
        check, _ = url_service.check_url(body.url)
    except UrlImportError as exc:
        raise _url_error(exc) from None
    return check.public()


@app.post("/api/videos/url/import", dependencies=[Depends(require_access), Depends(url_rate_limit)],
          status_code=202)
def import_video_url(body: UrlImportIn) -> dict:
    """Start an authorised retrieval; the job then follows the normal upload pipeline."""
    if not body.rights_confirmed:
        raise HTTPException(status_code=400, detail=UrlImportError("rights_not_confirmed").message)
    options = _job_options(body.title, body.language, body.style, body.slide_count, body.ai_images,
                           body.image_density, body.layout_mode, body.google_slides)
    if _busy():
        raise HTTPException(status_code=429, detail=UrlImportError("busy").message)
    try:
        check, parsed = url_service.check_url(body.url)  # never trust a client-side validation result
    except UrlImportError as exc:
        raise _url_error(exc) from None
    if not check.retrieval_permitted:
        raise HTTPException(status_code=403, detail=check.message)

    name = check.title or f"Video {check.platform_label} {check.video_id or ''}".strip()
    source = {
        "type": "url", "platform": check.platform, "url": check.url, "kind": parsed.kind,
        "video_id": parsed.video_id, "method": check.retrieval_method, "title": check.title,
        "thumbnail": check.thumbnail, "rights_confirmed_at": time.time(),
    }
    job_id, _ = jobs.new_job(options, _safe_display_name(f"{name}.mp4"), source=source)
    url_service.submit_download(job_id)
    return {"job_id": job_id, "queue_position": url_service.pending_downloads() + jobs.pending_count()}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_access)])
def get_job(job_id: str) -> dict:
    return _public(_load_job(job_id))


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_access)], status_code=202)
def retry_job(job_id: str) -> dict:
    job = _load_job(job_id)
    if job["status"] not in ("failed", "cancelled"):
        raise HTTPException(status_code=409, detail="Chỉ có thể thử lại công việc bị lỗi hoặc đã hủy.")
    d = jobs.job_dir(job_id)
    has_input = (d / INPUT_NAME).exists() or (d / "transcript.json").exists()
    from_url = (job.get("source") or {}).get("type") == "url"
    if not has_input and not from_url:
        raise HTTPException(status_code=410, detail="Dữ liệu gốc đã bị xóa. Hãy tải video lên lại.")
    if _busy():
        raise HTTPException(status_code=429, detail="Hệ thống đang bận, vui lòng thử lại sau.")
    if not has_input:
        # URL import that failed while downloading: retrieve again (the worker re-checks the policy).
        url_service.submit_download(job_id)
        return {"job_id": job_id, "queue_position": url_service.pending_downloads()}
    return {"job_id": job_id, "queue_position": jobs.enqueue(job_id)}


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_access)])
def cancel_job(job_id: str) -> dict:
    """Stop a queued or running job. Running jobs stop at the next safe point (GPU work is cancelled)."""
    job = _load_job(job_id)
    if job["status"] not in ("queued", "running"):
        raise HTTPException(status_code=409, detail="Công việc không còn đang chạy.")
    job = jobs.request_cancel(job_id)
    return {"status": job["status"], "cancel_requested": bool(job.get("cancel_requested"))}


@app.get("/api/jobs/{job_id}/preview", dependencies=[Depends(require_access)])
def list_previews(job_id: str) -> dict:
    """Slide images rendered by PowerPoint/LibreOffice (rendered on demand when missing)."""
    job = _load_job(job_id)
    d = jobs.job_dir(job_id)
    pptx = output_path(d)
    if job["status"] != "done" or not pptx.exists():
        raise HTTPException(status_code=404, detail="Bản trình chiếu chưa sẵn sàng.")
    if not preview.engine():
        return {"available": False, "engine": "", "slides": [],
                "reason": "Máy chưa cài PowerPoint hoặc LibreOffice nên không tạo được ảnh xem trước."}
    try:
        files = preview.render_previews(d, pptx)
    except preview.PreviewError as exc:
        return {"available": False, "engine": preview.engine(), "slides": [], "reason": str(exc)}
    version = int(pptx.stat().st_mtime)
    return {"available": True, "engine": preview.engine(),
            "slides": [f"/api/jobs/{job_id}/preview/{i}.png?v={version}" for i in range(1, len(files) + 1)]}


@app.get("/api/jobs/{job_id}/preview/{number}.png", dependencies=[Depends(require_access)])
def get_preview(job_id: str, number: int):
    _load_job(job_id)
    if not 1 <= number <= 99:
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh xem trước.")
    path = preview.preview_dir(jobs.job_dir(job_id)) / f"slide-{number:02d}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh xem trước.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/jobs/{job_id}/transcript", dependencies=[Depends(require_access)])
def get_transcript(job_id: str):
    _load_job(job_id)
    path = jobs.job_dir(job_id) / "transcript.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Chưa có bản ghi lời nói.")
    return FileResponse(path, media_type="application/json")


@app.get("/api/jobs/{job_id}/transcript.txt", dependencies=[Depends(require_access)])
def get_transcript_txt(job_id: str):
    _load_job(job_id)
    path = jobs.job_dir(job_id) / "transcript.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Chưa có bản ghi lời nói.")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}/download", dependencies=[Depends(require_access)])
def download(job_id: str):
    job = _load_job(job_id)
    path = output_path(jobs.job_dir(job_id))
    if job["status"] != "done" or not path.exists():
        raise HTTPException(status_code=404, detail="Bản trình chiếu chưa sẵn sàng.")
    stem = Path(job.get("filename") or "presentation").stem or "presentation"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{stem}.pptx",
    )


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_access)], status_code=204)
def delete_job(job_id: str):
    job = _load_job(job_id)
    if job["status"] in ("running", "downloading"):
        raise HTTPException(status_code=409, detail="Công việc đang chạy, không thể xóa.")
    jobs.delete(job_id)
    library.delete(job_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------- AI illustrations

SLIDE_ID_RE = re.compile(r"^slide-\d{2}$")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _illustration_entry(job_id: str, slide_id: str) -> tuple[Path, dict]:
    if not SLIDE_ID_RE.match(slide_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh minh họa.")
    d = jobs.job_dir(job_id) / "illustrations"
    manifest = _read_json(d / "manifest.json")
    if slide_id not in manifest:
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh minh họa.")
    return d, manifest[slide_id]


@app.get("/api/jobs/{job_id}/illustrations", dependencies=[Depends(require_access)])
def list_illustrations(job_id: str) -> dict:
    job = _load_job(job_id)
    manifest = _read_json(jobs.job_dir(job_id) / "illustrations" / "manifest.json")
    items = []
    for sid, e in sorted(manifest.items()):
        req = e.get("request") or {}
        items.append({
            "slide_id": sid, "slide_title": req.get("slide_title", ""), "state": e.get("state"),
            "error": e.get("error"), "source": e.get("source"), "has_image": e.get("state") == "done",
        })
    regen = (job.get("options") or {}).get("illustration_variants") or {}
    return {"items": items, "regenerations_used": sum(regen.values()),
            "regenerations_max": settings.image_max_regenerations_per_job}


@app.get("/api/jobs/{job_id}/illustrations/{slide_id}.jpg", dependencies=[Depends(require_access)])
def get_illustration(job_id: str, slide_id: str):
    _load_job(job_id)
    d, entry = _illustration_entry(job_id, slide_id)
    path = d / f"{slide_id}.jpg"
    if entry.get("state") != "done" or not path.is_file():
        raise HTTPException(status_code=404, detail="Ảnh chưa sẵn sàng.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/jobs/{job_id}/illustrations/{slide_id}/regenerate", dependencies=[Depends(require_access)],
          status_code=202)
def regenerate_illustration(job_id: str, slide_id: str) -> dict:
    """Redraw one illustration (new seed) and rebuild the deck; transcript and outline are reused."""
    job = _load_job(job_id)
    if job["status"] not in ("done", "failed"):
        raise HTTPException(status_code=409, detail="Công việc đang xử lý, hãy đợi hoàn tất.")
    _illustration_entry(job_id, slide_id)
    opts = job.get("options") or {}
    if not (opts.get("illustrations") or {}).get("enabled"):
        raise HTTPException(status_code=409, detail="Công việc này không bật ảnh minh họa AI.")
    variants = dict(opts.get("illustration_variants") or {})
    if sum(variants.values()) >= settings.image_max_regenerations_per_job:
        raise HTTPException(status_code=429, detail="Đã đạt số lần tạo lại ảnh tối đa cho bài này.")
    av = image_usage.availability()
    if not av.available:
        raise HTTPException(status_code=409, detail=av.reason)
    if not (jobs.job_dir(job_id) / "outline.json").exists():
        raise HTTPException(status_code=410, detail="Dữ liệu dàn ý đã bị xóa, không thể tạo lại ảnh.")
    if _busy():
        raise HTTPException(status_code=429, detail="Hệ thống đang bận, vui lòng thử lại sau.")
    variants[slide_id] = variants.get(slide_id, 0) + 1
    jobs.update(job_id, options={**opts, "illustration_variants": variants})
    return {"job_id": job_id, "queue_position": jobs.enqueue(job_id)}


# ---------------------------------------------------------------------- Google account

def same_origin(request: Request) -> None:
    """State-changing Google endpoints: refuse cross-site requests from other web pages."""
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Yêu cầu không hợp lệ.")


@app.get("/api/google/status", dependencies=[Depends(require_access)])
def google_status() -> dict:
    return google_auth.status()


@app.post("/api/google/connect", dependencies=[Depends(require_access), Depends(same_origin)])
def google_connect(request: Request) -> dict:
    """Start the OAuth consent flow; the browser navigates to the returned Google URL."""
    if not settings.google_slides_enabled:
        raise HTTPException(status_code=404, detail="Tính năng Google Slides đang tắt.")
    try:
        redirect = google_auth.redirect_uri_for(str(request.base_url))
        return {"auth_url": google_auth.start_authorization(redirect)}
    except GoogleError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.get("/api/google/oauth/callback", response_class=HTMLResponse)
def google_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    """Redirect target of Google's consent screen (protected by the one-time state + PKCE)."""
    if error:
        msg = "Bạn đã hủy kết nối Google." if error == "access_denied" else "Google báo lỗi khi đăng nhập."
        return _callback_page(False, msg)
    try:
        google_auth.finish_authorization(state, code)
    except GoogleError as exc:
        return _callback_page(False, str(exc))
    except Exception:
        log.exception("Google OAuth callback failed")
        return _callback_page(False, "Không hoàn tất được đăng nhập Google.")
    return _callback_page(True, "Đã kết nối tài khoản Google.")


def _callback_page(ok: bool, message: str) -> HTMLResponse:
    from html import escape
    target = "/#google=connected" if ok else "/#google=error"
    body = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Video2Slides</title>
<meta http-equiv="refresh" content="2;url={target}"></head>
<body style="font-family:Segoe UI,system-ui,sans-serif;display:grid;place-items:center;min-height:90vh">
<div style="text-align:center"><h2>{escape(message)}</h2><p>Đang quay lại ứng dụng…</p>
<p><a href="{target}">Quay lại ngay</a></p></div></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400, headers={"Cache-Control": "no-store",
                                                                       "Referrer-Policy": "no-referrer"})


@app.post("/api/google/disconnect", dependencies=[Depends(require_access), Depends(same_origin)])
def google_disconnect() -> dict:
    """Revoke the app's access and delete local tokens. Presentations already in Drive are kept."""
    google_auth.disconnect()
    return google_auth.status()


class GoogleExportIn(BaseModel):
    new_copy: bool = False


@app.post("/api/jobs/{job_id}/google", dependencies=[Depends(require_access), Depends(same_origin)],
          status_code=202)
def google_export(job_id: str, body: GoogleExportIn) -> dict:
    """Create the Google Slides version of a finished deck (or a new copy). Never overwrites one."""
    job = _load_job(job_id)
    if job["status"] != "done" or not output_path(jobs.job_dir(job_id)).exists():
        raise HTTPException(status_code=409, detail="Bài trình chiếu chưa sẵn sàng.")
    if (job.get("google") or {}).get("state") in ("queued", "running"):
        raise HTTPException(status_code=409, detail="Đang tạo bản Google Slides, vui lòng đợi.")
    if not google_ready():
        raise HTTPException(status_code=409, detail="Chưa kết nối tài khoản Google.")
    submit_google_export(job_id, body.new_copy)
    return {"job_id": job_id, "state": "queued"}


@app.post("/api/jobs/{job_id}/google/check", dependencies=[Depends(require_access), Depends(same_origin)])
def google_check(job_id: str) -> dict:
    """Compare the live Google Slides file with this app's slide data (read-only)."""
    pres, _, meta = _reading_source(job_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Bài này chưa có bản Google Slides.")
    try:
        res = check_sync(meta)
    except GoogleError as exc:
        raise HTTPException(status_code=409 if exc.code in ("not_connected", "revoked") else 502,
                            detail=str(exc)) from None
    expected = {m["object_id"]: speaker_notes_text(s, pres.language)
                for m, s in zip(meta.get("slides") or [], pres.slides)}
    edited = [m["slide_id"] for m in meta.get("slides") or []
              if m["object_id"] in (res.get("notes") or {})
              and " ".join(res["notes"][m["object_id"]].split()) != " ".join(expected[m["object_id"]].split())]
    return {"exists": res["exists"], "missing": res["missing"], "reordered": res["reordered"],
            "added": res["added"], "notes_edited": edited,
            "in_sync": res["exists"] and not (res["missing"] or res["reordered"] or res["added"] or edited)}


# ---------------------------------------------------------------------- Reading Mode / library

def _reading_source(job_id: str) -> tuple[Presentation, Transcript | None, dict | None]:
    """Outline, transcript and Google mapping from the job folder, or from the library."""
    try:
        d = jobs.job_dir(job_id)
    except jobs.JobNotFound:
        raise HTTPException(status_code=404, detail="Không tìm thấy bài trình chiếu.") from None
    entry = library.get(job_id)
    google_meta = ((jobs.load(job_id).get("google") or {}).get("meta") if (d / "job.json").exists() else None)         or (entry or {}).get("google")
    if (d / "outline.json").exists():
        pres, transcript = load_outline(d)
        return pres, transcript, google_meta
    if entry:
        pres = Presentation.model_validate_json(entry["outline_json"])
        transcript = Transcript.from_json(json.loads(entry["transcript_json"])) if entry.get("transcript_json") else None
        from .reading import ensure_reading_notes
        ensure_reading_notes(pres, transcript)
        return pres, transcript, google_meta
    raise HTTPException(status_code=404, detail="Không tìm thấy bài trình chiếu.")


@app.get("/api/jobs/{job_id}/reading", dependencies=[Depends(require_access)])
def reading(job_id: str) -> dict:
    """Slides + explanations for Reading Mode. The client owns the current slide index."""
    pres, transcript, gmeta = _reading_source(job_id)
    d = jobs.job_dir(job_id)
    pptx = output_path(d)
    images: list[str | None] = [None] * len(pres.slides)
    source = None
    if pptx.exists() and preview.engine():
        try:
            files = preview.render_previews(d, pptx)
            if len(files) == len(pres.slides):
                v = int(pptx.stat().st_mtime)
                images = [f"/api/jobs/{job_id}/preview/{i}.png?v={v}" for i in range(1, len(files) + 1)]
                source = "local"
        except preview.PreviewError as exc:
            log.info("reading previews unavailable: %s", exc)
    if source is None and gmeta and len(gmeta.get("slides") or []) == len(pres.slides):
        images = [f"/api/jobs/{job_id}/google/thumbnail/{i}.png" for i in range(1, len(pres.slides) + 1)]
        source = "google"
    slides = []
    for s, img in zip(pres.slides, images):
        item = slide_payload(s, transcript)
        item["image"] = img
        slides.append(item)
    return {
        "job_id": job_id, "title": pres.presentation_title, "language": pres.language,
        "slide_count": len(slides), "image_source": source, "slides": slides,
        "google_url": (gmeta or {}).get("url"), "pptx_available": pptx.exists(),
    }


@app.get("/api/jobs/{job_id}/google/thumbnail/{number}.png", dependencies=[Depends(require_access)])
def google_thumbnail(job_id: str, number: int):
    """Slide image from Google (cached on disk; Google's temporary URL never reaches the browser)."""
    _, _, meta = _reading_source(job_id)
    if not meta or not 1 <= number <= len(meta.get("slides") or []):
        raise HTTPException(status_code=404, detail="Không có ảnh slide.")
    cache = settings.data_dir / "library" / job_id / "thumbs" / f"{meta['presentation_id'][:16]}-{number:02d}.png"
    if not cache.is_file():
        try:
            fetch_thumbnail(meta, number - 1, cache)
        except GoogleError as exc:
            raise HTTPException(status_code=502 if exc.code != "not_found" else 404, detail=str(exc)) from None
    return FileResponse(cache, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/library", dependencies=[Depends(require_access)])
def library_list() -> dict:
    items = library.recent(30)
    for it in items:
        try:
            it["available"] = (jobs.job_dir(it["job_id"]) / "outline.json").exists()
        except jobs.JobNotFound:
            it["available"] = False
    return {"items": items}


# ---------------------------------------------------------------------- admin (GPU cost control)

def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Admin API is disabled unless ADMIN_TOKEN is set; it never shares the user password."""
    if not settings.admin_token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), settings.admin_token.encode()):
        raise HTTPException(status_code=401, detail="Sai hoặc thiếu ADMIN_TOKEN.")


class KillSwitchIn(BaseModel):
    active: bool


@app.get("/api/admin/images/usage", dependencies=[Depends(require_admin)])
def admin_image_usage() -> dict:
    return image_usage.report()


@app.post("/api/admin/images/kill-switch", dependencies=[Depends(require_admin)])
def admin_kill_switch(body: KillSwitchIn) -> dict:
    image_usage.set_kill_switch(body.active)
    log.warning("AI image kill switch set to %s", body.active)
    return {"kill_switch": image_usage.kill_switch_active()}


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Frontend files are small: always revalidate (ETag) so an app update is never served stale."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
