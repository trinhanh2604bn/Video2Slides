"""End-to-end processing of one job:
video -> audio/frames -> transcript -> outline -> [illustration plan -> AI images] -> pptx
-> [native Google Slides + speaker notes].

Intermediate results (transcript, frames, outline, generated images) are cached in
the job folder so a retry after e.g. a Gemini rate limit does not re-run Whisper,
and a retry after a GPU failure does not regenerate images that already exist.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, jobs, library, preview
from .ai import AIError, OutlineRequest, Presentation, get_provider
from .illustrations import (
    IllustrationOutcome, IllustrationService, ImageGenerationError, apply_illustrations, get_image_provider,
    plan_illustrations,
)
from .illustrations.planner import image_cap
from .google_slides import GoogleError
from .google_slides import auth as google_auth
from .google_slides.export import export_presentation
from .pptx_engine import FrameRef, build_pptx, validate_pptx
from .reading import ensure_reading_notes
from .transcribe import Transcript, TranscriptionError, transcribe
from .video import MediaError, extract_audio, extract_frames
from .sysinfo import peak_memory_mb

log = logging.getLogger(__name__)

INPUT_NAME = "input.mp4"
OUTPUT_NAME = "presentation.pptx"


def output_path(job_dir: Path) -> Path:
    return job_dir / OUTPUT_NAME


def illustrations_requested(opts: dict) -> bool:
    return bool(config.settings.ai_images_enabled and (opts.get("illustrations") or {}).get("enabled"))


def load_outline(d: Path) -> tuple[Presentation, Transcript | None]:
    """Cached outline with reading notes completed from the transcript (for decks made before them)."""
    pres = Presentation.model_validate_json((d / "outline.json").read_text(encoding="utf-8"))
    transcript = None
    if (d / "transcript.json").exists():
        transcript = Transcript.from_json(json.loads((d / "transcript.json").read_text(encoding="utf-8")))
    ensure_reading_notes(pres, transcript)
    return pres, transcript


def run_job(job_id: str) -> None:
    d = jobs.job_dir(job_id)
    job = jobs.update(job_id, status="running", started_at=time.time(), finished_at=None, error=None)
    opts = job["options"]
    want_images = illustrations_requested(opts)
    density = (opts.get("illustrations") or {}).get("density") or "standard"
    video = d / INPUT_NAME
    duration = (job.get("media") or {}).get("duration", 0.0)

    try:
        # 1-2. Audio + frames (skipped when cached from a previous attempt)
        jobs.set_stage(job_id, "extracting_audio")
        transcript_json = d / "transcript.json"
        frames_meta = d / "frames.json"
        if not transcript_json.exists() or not frames_meta.exists():
            if not video.exists():
                raise MediaError("Tệp video gốc đã bị xóa. Hãy tải video lên lại.")
            frames = extract_frames(video, d / "frames", duration)
            frames_meta.write_text(
                json.dumps([{"file": f.path.name, "timestamp": f.timestamp} for f in frames]), encoding="utf-8"
            )
            if not transcript_json.exists():
                audio = extract_audio(video, d / "audio.wav")
                jobs.check_cancel(job_id)
                # 3. Speech recognition
                jobs.set_stage(job_id, "transcribing")

                def on_progress(done: float, total: float) -> None:
                    jobs.check_cancel(job_id)
                    jobs.update(job_id, progress={"stage": "transcribing", "done_sec": round(done, 1),
                                                  "total_sec": round(total, 1)})

                try:
                    transcript = transcribe(audio, opts.get("language"), progress=on_progress)
                finally:
                    audio.unlink(missing_ok=True)
                transcript.save(d)
                jobs.update(job_id, progress=None)
        transcript = Transcript.from_json(json.loads(transcript_json.read_text(encoding="utf-8")))
        frame_refs = [
            FrameRef(d / "frames" / f["file"], float(f["timestamp"]))
            for f in json.loads(frames_meta.read_text(encoding="utf-8"))
            if (d / "frames" / f["file"]).exists()
        ]

        # 4. AI outline (cached)
        jobs.check_cancel(job_id)
        jobs.set_stage(job_id, "analyzing")
        outline_file = d / "outline.json"
        provider = get_provider()
        outline_cached = outline_file.exists()
        if outline_cached:
            pres = Presentation.model_validate_json(outline_file.read_text(encoding="utf-8"))
        else:
            pres = provider.generate(OutlineRequest(
                transcript=transcript,
                language=opts.get("language") or "vi",
                slide_count=opts.get("slide_count"),
                title_hint=opts.get("title") or "",
                style=opts.get("style") or "professional",
                frame_timestamps=tuple(f.timestamp for f in frame_refs),
                max_illustrations=image_cap(opts.get("slide_count") or opts.get("recommended_slides") or 10,
                                            density, config.settings.image_max_per_presentation)
                if want_images else 0,
            ))
            outline_file.write_text(pres.model_dump_json(indent=2), encoding="utf-8")
        ensure_reading_notes(pres, transcript)

        # 5. AI illustrations (optional, feature-flagged)
        outcome: IllustrationOutcome | None = None
        if want_images:
            outcome = _illustrate(job_id, d, pres, opts, density)
            apply_illustrations(pres, set(outcome.images))

        # 6. Slides
        jobs.check_cancel(job_id)
        jobs.set_stage(job_id, "generating")
        out = output_path(d)
        layout_mode = opts.get("layout_mode") or "presentation"
        warnings = build_pptx(pres, out, opts.get("style") or "professional", frame_refs,
                              illustrations=outcome.images if outcome else None, layout_mode=layout_mode)

        # 6. Validate before exposing the download
        jobs.set_stage(job_id, "finalizing")
        report = validate_pptx(out, expected_slides=len(pres.slides))
        if not report.ok:
            out.unlink(missing_ok=True)
            raise RuntimeError("Tệp PowerPoint tạo ra không hợp lệ: " + "; ".join(report.errors[:3]))
        preview_count = _render_previews(d, out)

        notices = []
        if not provider.is_llm:
            notices.append("Chưa cấu hình GEMINI_API_KEY nên dàn ý được tạo bằng chế độ offline đơn giản.")
        if outcome is not None:
            notices.extend(_illustration_notices(outcome))

        # Free disk: the source video and frames are no longer needed.
        video.unlink(missing_ok=True)
        _save_to_library(job_id, d, pres, opts, layout_mode, report.slide_count)

        # 7. Native Google Slides (optional). A Google failure never loses the finished .pptx.
        if opts.get("google_slides") or (jobs.load(job_id).get("google") or {}).get("meta"):
            jobs.check_cancel(job_id)
            jobs.set_stage(job_id, "exporting_google")
            google = _export_google(job_id, d, pres, report.pictures, new_copy=False, auto=True)
            if google.get("state") == "failed":
                notices.append(f"Chưa tạo được bản Google Slides: {google.get('error')} "
                               "Tệp PowerPoint vẫn sẵn sàng; bấm 'Tạo trên Google Slides' để thử lại.")
            elif google.get("state") == "stale":
                notices.append("Bài trình chiếu vừa được dựng lại. Bản Google Slides cũ được giữ nguyên "
                               "(không ghi đè chỉnh sửa của bạn); bấm 'Tạo bản Google Slides mới' nếu cần.")

        jobs.set_stage(job_id, "completed")
        job = jobs.load(job_id)
        jobs.update(
            job_id,
            status="done",
            finished_at=time.time(),
            result={
                "title": pres.presentation_title,
                "slide_count": report.slide_count,
                "pictures": report.pictures,
                "provider": provider.name,
                "model": getattr(provider, "used_model", None) if not outline_cached else None,
                "language": transcript.language,
                "layouts": [s.layout_type for s in pres.slides],
                "layout_mode": layout_mode,
                "warnings": (warnings + report.warnings)[:10],
                "notices": notices,
                "processing_seconds": round(time.time() - (job.get("started_at") or time.time()), 1),
                "illustrations": outcome.summary() if outcome else None,
                "preview_slides": preview_count,
                "peak_memory_mb": peak_memory_mb(),
            },
        )
    except jobs.JobCancelled:
        _cancelled(job_id)
    except ImageGenerationError as exc:
        if exc.code == "cancelled":
            _cancelled(job_id)
        else:
            _fail(job_id, str(exc))
    except (MediaError, TranscriptionError, AIError) as exc:
        _fail(job_id, str(exc))
    except Exception as exc:
        log.exception("job %s failed", job_id)
        _fail(job_id, f"Lỗi không mong muốn: {exc}")


def _illustrate(job_id: str, d: Path, pres: Presentation, opts: dict, density: str) -> IllustrationOutcome:
    """Plan and generate the deck's illustrations; applies the IMAGE_ON_FAILURE policy."""
    s = config.settings
    jobs.set_stage(job_id, "planning_illustrations")
    plan = plan_illustrations(pres, job_id, opts.get("style") or "professional", density,
                              variants=opts.get("illustration_variants") or {})
    (d / "illustrations").mkdir(exist_ok=True)
    (d / "illustrations" / "plan.json").write_text(
        json.dumps([r.to_json() for r in plan.requests], ensure_ascii=False, indent=2), encoding="utf-8")

    jobs.set_stage(job_id, "generating_images")
    jobs.update(job_id, illustrations={"total": len(plan.requests), "done": 0, "failed": 0, "in_progress": 0})
    if not plan.requests:
        return IllustrationOutcome(skipped=plan.skipped)

    try:
        provider = get_image_provider()
    except ImageGenerationError as exc:
        outcome = IllustrationOutcome(requested=len(plan.requests), skipped=plan.skipped,
                                      failed=[{"slide_id": r.slide_id, "code": exc.code, "message": str(exc)}
                                              for r in plan.requests])
    else:
        # Budgets, daily/monthly limits and the kill switch are enforced inside run() before any
        # GPU submit; images already generated for this job are reused without touching the GPU.
        service = IllustrationService(d, provider, progress=lambda p: jobs.update(job_id, illustrations=p),
                                      should_cancel=lambda: bool(jobs.load(job_id).get("cancel_requested")))
        outcome = service.run(plan)
    if outcome.failed and s.image_on_failure != "skip":
        first = outcome.failed[0]["message"]
        raise ImageGenerationError(
            f"Không tạo được {len(outcome.failed)}/{outcome.requested} ảnh minh họa AI: {first} "
            "Bấm 'Thử lại' để tiếp tục (ảnh đã tạo được giữ lại), hoặc tắt ảnh minh họa AI.", "failed")
    return outcome


def _save_to_library(job_id: str, d: Path, pres: Presentation, opts: dict, layout_mode: str, count: int) -> None:
    """Remember the finished deck so Reading Mode can reopen it later (never fails the job)."""
    try:
        tj = d / "transcript.json"
        library.save(job_id, title=pres.presentation_title, language=pres.language,
                     style=opts.get("style") or "professional", layout_mode=layout_mode, slide_count=count,
                     outline_json=pres.model_dump_json(),
                     transcript_json=tj.read_text(encoding="utf-8") if tj.exists() else None,
                     source={k: v for k, v in (jobs.load(job_id).get("source") or {}).items()
                             if k in ("type", "platform", "url", "title")})
    except Exception:
        log.exception("could not save job %s to the library", job_id)


# ---------------------------------------------------------------------- Google Slides

_google_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google-export")
_google_lock = threading.Lock()


def _export_google(job_id: str, d: Path, pres: Presentation, pictures: int | None, new_copy: bool,
                   auto: bool = False) -> dict:
    """Run one export and record its state on the job (``job["google"]``) and in the library."""
    with _google_lock:  # one export at a time: bounded API usage, no duplicate creation
        previous = (jobs.load(job_id).get("google") or {}).get("meta")
        pptx = output_path(d)
        if auto and previous and not new_copy:
            from .google_slides.export import file_hash
            if previous.get("build_hash") != file_hash(pptx):
                # The deck was rebuilt (e.g. a regenerated image). Never overwrite a presentation the
                # user may have edited; they can ask for a new copy.
                state = {"state": "stale", "meta": previous, "error": None, "updated_at": time.time()}
                jobs.update(job_id, google=state)
                return state
        jobs.update(job_id, google={"state": "running", "meta": previous, "error": None, "step": "uploading",
                                    "updated_at": time.time()})

        def step(name: str) -> None:
            g = jobs.load(job_id).get("google") or {}
            jobs.update(job_id, google={**g, "step": name})

        try:
            meta = export_presentation(pptx, pres, job_id, expected_pictures=pictures, previous=previous,
                                       new_copy=new_copy, progress=step)
        except GoogleError as exc:
            log.warning("Google Slides export for job %s failed: %s (%s)", job_id, exc.code, exc)
            state = {"state": "failed", "meta": previous, "error": str(exc), "code": exc.code,
                     "updated_at": time.time()}
        except Exception as exc:
            log.exception("Google Slides export for job %s crashed", job_id)
            state = {"state": "failed", "meta": previous, "error": f"Lỗi không mong muốn: {exc.__class__.__name__}",
                     "code": "unexpected", "updated_at": time.time()}
        else:
            if meta["validation"]["errors"]:
                state = {"state": "failed", "meta": meta, "code": "validation",
                         "error": "Bản Google Slides tạo ra không đạt kiểm tra: "
                                  + "; ".join(meta["validation"]["errors"][:2]), "updated_at": time.time()}
            else:
                state = {"state": "done", "meta": meta, "error": None, "updated_at": time.time()}
            try:
                library.set_google(job_id, meta)
            except Exception:
                log.exception("could not store Google metadata for job %s", job_id)
        jobs.update(job_id, google=state)
        return state


def submit_google_export(job_id: str, new_copy: bool) -> None:
    """Export a finished job from the result screen (runs in the background, polled via the job)."""
    jobs.update(job_id, google={**(jobs.load(job_id).get("google") or {}), "state": "queued", "error": None,
                                "updated_at": time.time()})

    def run() -> None:
        try:
            d = jobs.job_dir(job_id)
            pres, _ = load_outline(d)
            pictures = (jobs.load(job_id).get("result") or {}).get("pictures")
            _export_google(job_id, d, pres, pictures, new_copy=new_copy)
        except jobs.JobNotFound:
            pass
        except Exception:
            log.exception("background Google export for job %s crashed", job_id)
            try:
                g = jobs.load(job_id).get("google") or {}
                jobs.update(job_id, google={**g, "state": "failed", "error": "Lỗi không mong muốn khi xuất."})
            except jobs.JobNotFound:
                pass

    _google_pool.submit(run)


def google_ready() -> bool:
    return google_auth.configured() and google_auth.load_token() is not None


def _illustration_notices(outcome: IllustrationOutcome) -> list[str]:
    notes = []
    if outcome.images and not outcome.real_model:
        notes.append("Ảnh minh họa là ảnh giả lập (IMAGE_PROVIDER=mock), không phải ảnh do AI tạo.")
    if outcome.failed:
        notes.append(f"{len(outcome.failed)} ảnh minh họa AI không tạo được; các slide đó dùng bố cục không có ảnh AI.")
    return notes


def _render_previews(d: Path, pptx: Path) -> int:
    """Best effort: slide images for the in-app preview (never fails the job)."""
    if not preview.engine():
        return 0
    try:
        return len(preview.render_previews(d, pptx))
    except preview.PreviewError as exc:
        log.warning("preview rendering skipped: %s", exc)
        return 0


def _cancelled(job_id: str) -> None:
    try:
        jobs.update(job_id, status="cancelled", finished_at=time.time(), cancel_requested=False, progress=None,
                    error="Đã hủy theo yêu cầu. Bấm 'Thử lại' để tiếp tục (các bước đã xong được giữ lại).")
    except jobs.JobNotFound:
        pass


def _fail(job_id: str, message: str) -> None:
    try:
        jobs.update(job_id, status="failed", finished_at=time.time(), error=message)
    except jobs.JobNotFound:
        pass
