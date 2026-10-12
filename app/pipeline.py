"""End-to-end processing of one job:
video -> audio/frames -> transcript -> outline -> [illustration plan -> AI images] -> pptx.

Intermediate results (transcript, frames, outline, generated images) are cached in
the job folder so a retry after e.g. a Gemini rate limit does not re-run Whisper,
and a retry after a GPU failure does not regenerate images that already exist.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import config, jobs
from .ai import AIError, OutlineRequest, Presentation, get_provider
from .illustrations import (
    IllustrationOutcome, IllustrationService, ImageGenerationError, apply_illustrations, get_image_provider,
    plan_illustrations,
)
from .illustrations.planner import image_cap
from .pptx_engine import FrameRef, build_pptx, validate_pptx
from .transcribe import Transcript, TranscriptionError, transcribe
from .video import MediaError, extract_audio, extract_frames

log = logging.getLogger(__name__)

INPUT_NAME = "input.mp4"
OUTPUT_NAME = "presentation.pptx"


def output_path(job_dir: Path) -> Path:
    return job_dir / OUTPUT_NAME


def illustrations_requested(opts: dict) -> bool:
    return bool(config.settings.ai_images_enabled and (opts.get("illustrations") or {}).get("enabled"))


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
                # 3. Speech recognition
                jobs.set_stage(job_id, "transcribing")
                try:
                    transcript = transcribe(audio, opts.get("language"))
                finally:
                    audio.unlink(missing_ok=True)
                transcript.save(d)
        transcript = Transcript.from_json(json.loads(transcript_json.read_text(encoding="utf-8")))
        frame_refs = [
            FrameRef(d / "frames" / f["file"], float(f["timestamp"]))
            for f in json.loads(frames_meta.read_text(encoding="utf-8"))
            if (d / "frames" / f["file"]).exists()
        ]

        # 4. AI outline (cached)
        jobs.set_stage(job_id, "analyzing")
        outline_file = d / "outline.json"
        provider = get_provider()
        if outline_file.exists():
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

        # 5. AI illustrations (optional, feature-flagged)
        outcome: IllustrationOutcome | None = None
        if want_images:
            outcome = _illustrate(job_id, d, pres, opts, density)
            apply_illustrations(pres, set(outcome.images))

        # 6. Slides
        jobs.set_stage(job_id, "generating")
        out = output_path(d)
        warnings = build_pptx(pres, out, opts.get("style") or "professional", frame_refs,
                              illustrations=outcome.images if outcome else None)

        # 6. Validate before exposing the download
        jobs.set_stage(job_id, "finalizing")
        report = validate_pptx(out, expected_slides=len(pres.slides))
        if not report.ok:
            out.unlink(missing_ok=True)
            raise RuntimeError("Tệp PowerPoint tạo ra không hợp lệ: " + "; ".join(report.errors[:3]))

        notices = []
        if not provider.is_llm:
            notices.append("Chưa cấu hình GEMINI_API_KEY nên dàn ý được tạo bằng chế độ offline đơn giản.")
        if outcome is not None:
            notices.extend(_illustration_notices(outcome))

        # Free disk: the source video and frames are no longer needed.
        video.unlink(missing_ok=True)

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
                "language": transcript.language,
                "layouts": [s.layout_type for s in pres.slides],
                "warnings": (warnings + report.warnings)[:10],
                "notices": notices,
                "processing_seconds": round(time.time() - (job.get("started_at") or time.time()), 1),
                "illustrations": outcome.summary() if outcome else None,
            },
        )
    except (MediaError, TranscriptionError, AIError, ImageGenerationError) as exc:
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
        service = IllustrationService(d, provider, progress=lambda p: jobs.update(job_id, illustrations=p))
        outcome = service.run(plan)
    if outcome.failed and s.image_on_failure != "skip":
        first = outcome.failed[0]["message"]
        raise ImageGenerationError(
            f"Không tạo được {len(outcome.failed)}/{outcome.requested} ảnh minh họa AI: {first} "
            "Bấm 'Thử lại' để tiếp tục (ảnh đã tạo được giữ lại), hoặc tắt ảnh minh họa AI.", "failed")
    return outcome


def _illustration_notices(outcome: IllustrationOutcome) -> list[str]:
    notes = []
    if outcome.images and not outcome.real_model:
        notes.append("Ảnh minh họa là ảnh giả lập (IMAGE_PROVIDER=mock), không phải ảnh do AI tạo.")
    if outcome.failed:
        notes.append(f"{len(outcome.failed)} ảnh minh họa AI không tạo được; các slide đó dùng bố cục không có ảnh AI.")
    return notes


def _fail(job_id: str, message: str) -> None:
    try:
        jobs.update(job_id, status="failed", finished_at=time.time(), error=message)
    except jobs.JobNotFound:
        pass
