"""FastAPI entry point: REST API + static single-page frontend."""

from __future__ import annotations

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
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import jobs
from .ai import recommend_slide_count
from .config import SUPPORTED_LANGUAGES, settings
from .illustrations import usage as image_usage
from .illustrations.planner import DENSITIES
from .pipeline import INPUT_NAME, output_path, run_job
from .pptx_engine import THEMES
from .url_import import service as url_service
from .url_import.errors import UrlImportError
from .video import MediaError, ffmpeg_available, validate

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
            "filename", "options", "media", "error", "result", "illustrations")
    out = {k: job.get(k) for k in keys}
    src = job.get("source") or {"type": "upload"}
    out["source"] = {k: src.get(k) for k in ("type", "platform", "url", "title", "thumbnail") if k in src}
    if job.get("status") == "queued":
        out["queue_length"] = jobs.pending_count()
    return out


def _safe_display_name(name: str | None) -> str:
    base = Path(name or "video.mp4").name
    base = re.sub(r"[^\w.\- ()\[\]]+", "_", base, flags=re.UNICODE).strip() or "video.mp4"
    return base[:120]


def _active_provider_name() -> str:
    if settings.ai_provider == "auto":
        return "gemini" if settings.gemini_api_key else "offline"
    return settings.ai_provider


@app.get("/api/health")
def health() -> dict:
    tools = ffmpeg_available()
    return {"ok": all(tools.values()), "ffmpeg": tools["ffmpeg"], "ffprobe": tools["ffprobe"]}


@app.get("/api/config")
def public_config() -> dict:
    return {
        "max_upload_mb": settings.max_upload_mb,
        "max_duration_sec": settings.max_duration_sec,
        "languages": SUPPORTED_LANGUAGES,
        "default_language": settings.default_language,
        "styles": {k: t.label for k, t in THEMES.items()},
        "default_style": "professional",
        "ai_provider": _active_provider_name(),
        "password_required": bool(settings.app_password),
        "whisper_model": settings.whisper_model,
        "ai_images": _ai_images_config(),
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
    slide_count: int = Form(default=0, ge=0, le=25),
    ai_images: bool = Form(default=False),
    image_density: str = Form(default="standard"),
) -> dict:
    """Upload an MP4 and queue it for processing. Runs in a worker thread (sync def)."""
    if _busy():
        raise HTTPException(status_code=429, detail="Hệ thống đang bận, vui lòng thử lại sau ít phút.")

    display = _safe_display_name(file.filename)
    if not display.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Chỉ hỗ trợ tệp .mp4.")
    options = _job_options(title, language, style, slide_count, ai_images, image_density)
    job_id, d = jobs.new_job(options, display)
    target = d / INPUT_NAME
    written = 0
    try:
        with target.open("wb") as fh:
            while chunk := file.file.read(CHUNK):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail=f"Tệp vượt quá {settings.max_upload_mb} MB.")
                fh.write(chunk)
        info = validate(target)
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

    if not options["slide_count"]:
        # Rough recommendation; the AI refines it from the transcript.
        options["recommended_slides"] = recommend_slide_count(info.duration, int(info.duration * 2.3))
    jobs.update(job_id, options=options, media={
        "duration": round(info.duration, 2), "width": info.width, "height": info.height,
        "video_codec": info.video_codec, "audio_codec": info.audio_codec, "size_bytes": info.size_bytes,
    })
    position = jobs.enqueue(job_id)
    return {"job_id": job_id, "queue_position": position}


def _busy() -> bool:
    return jobs.pending_count() + url_service.pending_downloads() >= settings.max_queue_size


def _job_options(title: str, language: str, style: str, slide_count: int, ai_images: bool = False,
                 image_density: str = "standard") -> dict:
    language = language or settings.default_language
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
            "illustrations": {"enabled": bool(ai_images), "density": image_density}}


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
    slide_count: int = Field(default=0, ge=0, le=25)
    ai_images: bool = False
    image_density: str = "standard"


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
                           body.image_density)
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
    if job["status"] != "failed":
        raise HTTPException(status_code=409, detail="Chỉ có thể thử lại công việc bị lỗi.")
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
