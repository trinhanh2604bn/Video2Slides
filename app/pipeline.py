"""End-to-end processing of one job:
video -> audio/frames -> transcript -> outline -> [illustration plan -> AI images] -> pptx
-> [native Google Slides + speaker notes].

Intermediate results (transcript, frames, LLM checkpoints, outline, generated images) are cached
in the job folder so a retry after an LLM failure (Ollama stopped, timeout, invalid answer) does not
re-run FFmpeg or Whisper, and a retry after a GPU failure does not regenerate existing images.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, jobs, library, preview
from .ai import AIError, OutlineRequest, Presentation, get_provider, recommend_slide_count
from .ai.grounding import sanitize as sanitize_grounding
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
from .media_remote import (
    TRANSCRIBE_IMPL_VERSION, ModalMediaClient, RemoteMediaError, Waiter, file_sha256, save_frames,
    transcript_key,
)
from .transcribe import Segment, Transcript, TranscriptionError, release_model, transcribe
from . import media_local
from .video import MediaError, extract_audio, extract_frames, select_frames
from .sysinfo import peak_memory_mb, system_memory_mb

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
        # 1-3. Audio, frames and transcript (cached per step; on Modal or on this computer)
        transcript, frame_refs = _prepare_media(job_id, d, video, opts, duration)

        # 4. AI outline (cached; reused only when made by the same provider/model)
        jobs.check_cancel(job_id)
        jobs.set_stage(job_id, "analyzing")
        outline_file = d / "outline.json"
        provider = get_provider()
        outline_cached = _outline_reusable(d, provider)
        llm_info = None
        if outline_cached:
            pres = Presentation.model_validate_json(outline_file.read_text(encoding="utf-8"))
            llm_info = _read_json(d / "outline_meta.json").get("llm")
        else:
            if provider.name == "ollama" and config.settings.release_whisper_before_llm and release_model():
                log.info("Whisper model released before local LLM inference")
            mem_before = system_memory_mb()
            llm_started = time.time()
            try:
                pres = provider.generate(OutlineRequest(
                    status=lambda message: _status(job_id, message),
                    transcript=transcript,
                    language=opts.get("language") or "vi",
                    slide_count=opts.get("slide_count"),
                    title_hint=opts.get("title") or "",
                    style=opts.get("style") or "professional",
                    frame_timestamps=tuple(f.timestamp for f in frame_refs),
                    max_illustrations=image_cap(opts.get("slide_count") or opts.get("recommended_slides") or 10,
                                                density, config.settings.image_max_per_presentation)
                    if want_images else 0,
                    cache_dir=d,
                    progress=lambda step, done, total: _llm_progress(job_id, step, done, total),
                    check_cancel=lambda: jobs.check_cancel(job_id),
                ))
            finally:
                if hasattr(provider, "describe"):
                    llm_info = {**provider.describe(), "wall_seconds": round(time.time() - llm_started, 1),
                                "system_memory_before_mb": mem_before, "system_memory_after_mb": system_memory_mb()}
                    jobs.update(job_id, llm=llm_info)
            # Final grounding net for every provider: no invented boilerplate slides / contact details.
            grounding_notices = sanitize_grounding(pres, transcript.text)
            outline_file.write_text(pres.model_dump_json(indent=2), encoding="utf-8")
            used = getattr(provider, "used_provider", None) or provider.name
            (d / "outline_meta.json").write_text(json.dumps(
                {"provider": used, "route_mode": provider.name, "model": getattr(provider, "used_model", None),
                 "grounding": grounding_notices,
                 "prompt_version": (llm_info or {}).get("prompt_version"), "schema": "presentation-v1",
                 "transcript_key": _read_json(d / "transcript_meta.json").get("key"),
                 "created_at": time.time(), "llm": llm_info},
                ensure_ascii=False, indent=2), encoding="utf-8")
            _status(job_id, None)
            jobs.update(job_id, progress=None)
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
            notices.append("LLM_PROVIDER=offline: dàn ý được tạo bằng chế độ offline đơn giản, không dùng mô hình AI.")
        notices.extend(f"Ghi chú từ bước AI: {w}" for w in ((llm_info or {}).get("warnings") or [])[:3])
        notices.extend((_read_json(d / "outline_meta.json").get("grounding") or [])[:5])
        if outcome is not None:
            notices.extend(_illustration_notices(outcome))

        # The pipeline no longer needs the source video: keep it (a rename, no copy) for in-app playback of the
        # Reading Mode timestamps until the periodic cleanup expires it, or delete it now.
        if config.settings.keep_source_video_hours > 0 and video.exists():
            video.replace(d / jobs.PLAYBACK_NAME)
        else:
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
                "provider": (llm_info or {}).get("provider") or getattr(provider, "used_provider", None)
                or provider.name,
                "model": (llm_info or {}).get("model") or getattr(provider, "used_model", None),
                "llm_route": _route_summary(llm_info),
                "media_processing": jobs.load(job_id).get("media_processing"),
                "llm": _llm_summary(llm_info),
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
    except AIError as exc:
        _fail(job_id, str(exc), getattr(exc, "code", "ai_error"))
    except (MediaError, TranscriptionError) as exc:
        _fail(job_id, str(exc))
    except RemoteMediaError as exc:
        hint = (" Đây là lỗi cấu hình GPU/CUDA của worker trên Modal; video gốc được giữ lại để thử lại sau khi "
                "sửa." if exc.code == "cuda_unavailable" else "")
        _fail(job_id, f"Xử lý video trên Modal thất bại: {exc}{hint}", exc.code)
    except Exception as exc:
        log.exception("job %s failed", job_id)
        _fail(job_id, f"Lỗi không mong muốn: {exc}")


# ---------------------------------------------------------------------- media: audio, frames, transcript

def _status(job_id: str, message: str | None) -> None:
    """Short Vietnamese status line shown under the current stage (never secrets or raw API answers)."""
    try:
        jobs.update(job_id, status_message=message)
    except jobs.JobNotFound:
        pass


def _transcript_settings(opts: dict, on_modal: bool) -> dict:
    s = config.settings
    common = {"language": opts.get("language") or s.default_language, "vad": True,
              "audio_normalize": s.audio_normalize}
    if on_modal:
        model = s.modal_whisper_model_en if common["language"] == "en" else s.modal_whisper_model
        return {"location": "modal", "model": model, "compute_type": s.modal_whisper_compute_type,
                "beam_size": s.modal_whisper_beam_size, "batch_size": s.modal_whisper_batch_size,
                "word_timestamps": s.modal_whisper_batch_size > 0, **common}
    return {"location": "local", "model": s.whisper_model, "compute_type": s.whisper_compute_type,
            "beam_size": s.whisper_beam_size, **common}


def _transcript_current(d: Path, tset: dict) -> bool:
    """transcript.json may be reused: made with the same source + settings (old jobs without meta: yes)."""
    meta = _read_json(d / "transcript_meta.json")
    if not meta:
        return True
    return meta.get("key") == transcript_key(meta.get("source_sha256") or "", **tset)


def _prepare_media(job_id: str, d: Path, video: Path, opts: dict, duration: float) -> tuple[Transcript, list[FrameRef]]:
    on_modal = config.settings.media_on_modal
    tset = _transcript_settings(opts, on_modal)
    transcript_json, frames_meta = d / "transcript.json", d / "frames.json"
    have = transcript_json.exists() and frames_meta.exists()
    if not have or (video.exists() and not _transcript_current(d, tset)):
        if not video.exists():
            raise MediaError("Tệp video gốc đã bị xóa. Hãy tải video lên lại.")
        src = jobs.load(job_id).get("content_hash") or file_sha256(video)
        key = transcript_key(src, **tset)
        if on_modal:
            _media_on_modal(job_id, d, video, opts, tset, src, key)
        else:
            _media_local(job_id, d, video, duration, tset, src, key)
    transcript = Transcript.from_json(json.loads(transcript_json.read_text(encoding="utf-8")))
    frame_refs = [
        FrameRef(d / "frames" / f["file"], float(f["timestamp"]))
        for f in json.loads(frames_meta.read_text(encoding="utf-8"))
        if (d / "frames" / f["file"]).exists()
    ]
    return transcript, frame_refs


def _write_frames_meta(d: Path, frames) -> None:
    (d / "frames.json").write_text(
        json.dumps([{"file": f.path.name, "timestamp": f.timestamp} for f in frames]), encoding="utf-8")


def _write_transcript_meta(d: Path, key: str, src: str, tset: dict, extra: dict) -> None:
    (d / "transcript_meta.json").write_text(json.dumps(
        {"key": key, "source_sha256": src, **tset, "impl": TRANSCRIBE_IMPL_VERSION, "created_at": time.time(),
         **extra}, ensure_ascii=False, indent=2), encoding="utf-8")


def _media_local(job_id: str, d: Path, video: Path, duration: float, tset: dict, src: str, key: str) -> None:
    """FFmpeg + faster-whisper on this computer (CPU)."""
    jobs.set_stage(job_id, "extracting_audio")
    t0 = time.time()
    frames = extract_frames(video, d / "frames", duration)
    _write_frames_meta(d, frames)
    audio = extract_audio(video, d / "audio.wav")
    audio_sec = round(time.time() - t0, 2)
    jobs.check_cancel(job_id)
    jobs.set_stage(job_id, "transcribing")

    def on_progress(done: float, total: float) -> None:
        jobs.check_cancel(job_id)
        jobs.update(job_id, progress={"stage": "transcribing", "done_sec": round(done, 1),
                                      "total_sec": round(total, 1)})

    t1 = time.time()
    try:
        transcript = transcribe(audio, tset["language"], progress=on_progress)
    finally:
        audio.unlink(missing_ok=True)
    transcript.save(d)
    info = {"location": "local", "ffmpeg": {"location": "local-cpu"},
            "whisper": {"device": "cpu", "model": tset["model"], "compute_type": tset["compute_type"]},
            "timings": {"audio_and_frames_sec": audio_sec, "transcribe_sec": round(time.time() - t1, 2)}}
    _write_transcript_meta(d, key, src, tset, {"processing": info})
    jobs.update(job_id, progress=None, media_processing=info)


def _media_on_modal(job_id: str, d: Path, video: Path, opts: dict, tset: dict, src: str, key: str) -> None:
    """Upload -> FFmpeg on a Modal CPU container -> faster-whisper on a Modal GPU; checkpoint after each step."""
    s = config.settings
    ck_file = d / "remote_media.json"
    ck = _read_json(ck_file)
    if ck.get("source_sha256") != src:
        ck = {"source_sha256": src, "timings": {}}

    def save() -> None:
        ck_file.write_text(json.dumps(ck, ensure_ascii=False, indent=2), encoding="utf-8")

    client = ModalMediaClient()
    waiter = Waiter(check_cancel=lambda: jobs.check_cancel(job_id))
    info: dict = {"location": "modal"}
    jobs.set_stage(job_id, "extracting_audio")
    # Audio-only upload: the audio track is copied locally with PyAV (no re-encoding, no FFmpeg install) and
    # frames are grabbed locally, so a long video is not uploaded whole. Otherwise the MP4 is sent.
    audio_only = s.modal_upload_audio_only and media_local.available()
    upload_file = video
    if audio_only and not ck.get("upload"):
        _status(job_id, "Đang tách âm thanh và khung hình trên máy (không mã hóa lại).")
        t0 = time.time()
        upload_file = media_local.remux_audio(video, d / "audio_upload.m4a")
        duration_local = media_local.probe(video).duration
        frames = select_frames(media_local.grab_frames(video, d / "frames", duration_local), d / "frames")
        _write_frames_meta(d, frames)
        ck["timings"]["local_audio_and_frames_sec"] = round(time.time() - t0, 2)
        ck["upload_bytes_saved"] = video.stat().st_size - upload_file.stat().st_size
    try:
        for attempt in range(2):  # an upload lost on the worker (expired/corrupt) is redone once
            try:
                if not ck.get("upload"):
                    _status(job_id, "Đang tải âm thanh lên Modal." if upload_file != video else "Đang tải video lên Modal.")
                    waiter.on_tick = lambda frac: jobs.update(job_id, progress={
                        "stage": "extracting_audio", "done": round(frac * 100), "total": 100})
                    up_sha = src if upload_file == video else file_sha256(upload_file)
                    ck["upload"] = client.upload(upload_file, up_sha, waiter)
                    ck["upload"]["audio_only"] = upload_file != video
                    ck["timings"]["upload_sec"] = ck["upload"]["upload_sec"]
                    save()
                if not ck.get("preprocessed"):
                    _status(job_id, "Đang tách âm thanh và khung hình bằng FFmpeg trên Modal (CPU).")
                    waiter.on_tick = None
                    t0 = time.time()
                    sent_audio_only = bool(ck["upload"].get("audio_only"))
                    params = {"max_duration_sec": s.max_duration_sec, "audio_normalize": tset["audio_normalize"],
                              "frame_candidates": 0 if sent_audio_only else s.max_frames * 2,
                              "frame_width": s.frame_width, "audio_only": sent_audio_only}
                    res = client.preprocess(ck["upload"], params, waiter)
                    if not sent_audio_only:
                        frames = save_frames(res, d / "frames")
                        _write_frames_meta(d, frames)
                    media = res.get("media") or {}
                    job = jobs.load(job_id)
                    options = job.get("options") or {}
                    if not options.get("slide_count") and not options.get("recommended_slides"):
                        options["recommended_slides"] = recommend_slide_count(media.get("duration", 0),
                                                                              int(media.get("duration", 0) * 2.3))
                    keys = ("duration", "audio_codec") if sent_audio_only else (
                        "duration", "width", "height", "video_codec", "audio_codec", "size_bytes")
                    jobs.update(job_id, options=options, media={**(job.get("media") or {}), **{
                        k: (round(v, 2) if k == "duration" else v) for k, v in media.items() if k in keys}})
                    ck["preprocessed"] = True
                    ck["media"] = media
                    ck["timings"].update(preprocess_round_trip_sec=round(time.time() - t0, 2),
                                         **{f"preprocess_{k}": v for k, v in (res.get("timings") or {}).items()})
                    ck["ffmpeg"] = res.get("ffmpeg")
                    save()
                break
            except RemoteMediaError as exc:
                if attempt or exc.code not in ("upload_incomplete", "upload_corrupt", "expired", "audio_missing"):
                    raise
                ck = {"source_sha256": src, "timings": {}}

        jobs.check_cancel(job_id)
        jobs.set_stage(job_id, "transcribing")
        _status(job_id, "Đang chờ Modal cấp GPU cho Whisper (có thể mất vài phút khi Modal đang bận).")
        waiter.on_started = lambda: _status(job_id, "Đang nhận dạng giọng nói bằng Whisper trên GPU (Modal).")
        info["llm_prewarm"] = _prewarm_llm(d)
        total = float((ck.get("media") or {}).get("duration") or 0)
        waiter.on_tick = lambda frac: jobs.update(job_id, progress={
            "stage": "transcribing", "done_sec": round(frac * total, 1), "total_sec": round(total, 1)})
        t0 = time.time()
        params = {"model": tset["model"], "compute_type": tset["compute_type"], "beam_size": tset["beam_size"],
                  "vad": tset["vad"], "language": None if tset["language"] == "auto" else tset["language"],
                  "batch_size": tset["batch_size"], "word_timestamps": tset["word_timestamps"],
                  "timestamps": not tset["word_timestamps"]}
        for attempt in range(2):
            try:
                res = client.transcribe(ck["upload"]["upload_id"], params, waiter)
                break
            except RemoteMediaError as exc:
                if attempt or not exc.retryable:
                    raise
        metrics = res.get("metrics") or {}
        if metrics.get("device") != "cuda":
            raise RemoteMediaError("Whisper trên Modal không chạy bằng CUDA; kết quả bị từ chối.", "cuda_unavailable")
        transcript = _checked_transcript(res, total)
        transcript.save(d)
        ck["timings"]["transcribe_round_trip_sec"] = round(time.time() - t0, 2)
        info.update(ffmpeg=ck.get("ffmpeg"), whisper=metrics, timings=ck["timings"],
                    language_probability=res.get("language_probability"))
        _write_transcript_meta(d, key, src, tset, {"processing": info})
        client.delete(ck["upload"]["upload_id"])
        ck_file.unlink(missing_ok=True)
        (d / "audio_upload.m4a").unlink(missing_ok=True)
        jobs.update(job_id, progress=None, media_processing=info)
        _status(job_id, None)
    except BaseException:
        # Cancellation or failure: remove the temporary files on Modal; the local video stays for 'Thử lại'.
        if ck.get("upload"):
            client.delete(ck["upload"]["upload_id"])
        ck_file.unlink(missing_ok=True)
        (d / "audio_upload.m4a").unlink(missing_ok=True)
        raise


def _prewarm_llm(d: Path) -> bool:
    """Start the Modal LLM GPU container while Whisper runs, only when this job will certainly use it
    (LLM_PROVIDER=modal_ollama, or auto with no Gemini key / pinned job / open circuit). Measured cold start
    ~100 s (Ollama GPU runner start + first generation), which then overlaps with transcription."""
    s = config.settings
    if not s.modal_llm_worker_url or (d / "outline.json").exists():
        return False
    try:
        from .ai import active_provider_name
        from .ai.modal_ollama import ModalOllamaClient
        from .ai.router import RoutingProvider

        name = active_provider_name()
        if name == "modal_ollama" or (name == "auto" and RoutingProvider().will_use_modal(d)):
            threading.Thread(target=lambda: ModalOllamaClient().warmup(), name="llm-prewarm", daemon=True).start()
            return True
    except Exception:
        log.exception("LLM pre-warm skipped")
    return False


def _checked_transcript(res: dict, duration: float) -> Transcript:
    """Segments ordered and inside the video; empty speech is an explicit error."""
    limit = (duration or float(res.get("duration") or 0)) + 1.0
    segs = []
    for s in sorted(res.get("segments") or [], key=lambda x: float(x.get("start", 0))):
        start, end = float(s.get("start", -1)), float(s.get("end", -1))
        text = str(s.get("text") or "").strip()
        if text and 0 <= start <= end <= limit:
            segs.append(Segment(round(start, 2), round(end, 2), text))
    if not segs:
        raise TranscriptionError("Không phát hiện được lời nói trong video.")
    return Transcript(segments=segs, language=str(res.get("language") or ""),
                      duration=float(res.get("duration") or duration))


def _route_summary(info: dict | None) -> dict | None:
    route = (info or {}).get("route")
    if not route:
        return None
    out = {k: route.get(k) for k in ("attempted", "successful_provider", "model", "fallback_reason",
                                     "fallback_reason_text", "timings", "warnings", "providers_used")}
    gem = (info or {}).get("gemini_attempts") or (info if route.get("successful_provider") == "gemini" else {})
    if gem:
        out["gemini_requests"] = gem.get("requests")
        out["gemini_repairs"] = gem.get("repairs")
    if (info or {}).get("modal"):
        out["modal"] = info["modal"]
    return out


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _outline_reusable(d: Path, provider) -> bool:
    """A cached outline is reused on retry unless it was made by a different provider/model.
    Outlines from before outline_meta.json existed are always reused (compatibility)."""
    if not (d / "outline.json").exists():
        return False
    meta = _read_json(d / "outline_meta.json")
    if not meta:
        return True
    tkey = _read_json(d / "transcript_meta.json").get("key")
    if meta.get("transcript_key") and tkey and meta["transcript_key"] != tkey:
        return False  # the transcript was redone: the outline was written from another input
    if provider.name == "auto":
        # Validated output of either routed provider is kept (with its provenance in outline_meta.json).
        return meta.get("provider") in ("gemini", "modal_ollama")
    if meta.get("provider") != provider.name:
        return False
    # Gemini may legitimately answer with a fallback model; for Ollama the model is part of the cache key.
    return provider.name != "ollama" or meta.get("model") == getattr(provider, "used_model", None)


def _llm_progress(job_id: str, step: str, done: int, total: int) -> None:
    """Real counts reported by the provider (parts analysed, slides written); never an estimate."""
    job = jobs.load(job_id)
    if job.get("stage") != step:
        jobs.set_stage(job_id, step)
    jobs.update(job_id, progress={"stage": step, "done": done, "total": total})


def _llm_summary(info: dict | None) -> dict | None:
    if not info:
        return None
    keys = ("provider", "model", "ollama_version", "num_ctx", "think", "requests", "cache_hits", "repairs",
            "splits", "prompt_tokens", "output_tokens", "llm_seconds", "wall_seconds", "cold_start_load_sec",
            "avg_tokens_per_sec", "transcript_parts", "system_memory_before_mb", "system_memory_after_mb")
    return {k: info.get(k) for k in keys if k in info}


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


def _fail(job_id: str, message: str, code: str | None = None) -> None:
    try:
        jobs.update(job_id, status="failed", finished_at=time.time(), error=message, error_code=code, progress=None)
    except jobs.JobNotFound:
        pass
