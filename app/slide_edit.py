"""Edit slides of a finished deck with plain-language instructions ("tách thành 2 ý", "đơn giản hóa slide 3").

The user queues several requests and applies them together, so a GPU cold start is paid once. Edits run on the
``slide-edit`` background thread, never in the job queue, and their LLM requests yield to a deck being made
(``app.ai.assist``). Each edited slide is rewritten by the same slide writer as the pipeline (same schema,
grounding checks against the transcript and bounded repairs), then the .pptx is rebuilt from cached frames and
illustrations (no FFmpeg, Whisper or image GPU). The previous outline is kept, so an edit can be undone.

A Google Slides copy is never overwritten: it is marked out of date and the user can create a new copy.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import jobs, pipeline
from .ai.grounding import sanitize as sanitize_grounding
from .ai.schema import Presentation, Slide
from .config import settings
from .illustrations import apply_illustrations
from .pptx_engine import FrameRef, build_pptx, validate_pptx

log = logging.getLogger(__name__)

MAX_EDITS = 6
MAX_INSTRUCTION = 400
HISTORY_KEEP = 10
HISTORY_DIR = "outline_history"
RUNNING = ("queued", "running")

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slide-edit")
_inflight: set[str] = set()
_lock = threading.Lock()


class EditError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------- public state + guards

def running(job: dict) -> bool:
    return (job.get("edit") or {}).get("state") in RUNNING and job.get("id") in _inflight


def public_state(job: dict) -> dict | None:
    e = job.get("edit")
    if not e:
        return None
    out = {k: e.get(k) for k in ("state", "kind", "step", "slides", "error", "notices", "message", "updated_at")}
    if out["state"] in RUNNING and job.get("id") not in _inflight:
        out.update(state="failed", error="Bị gián đoạn (máy chủ khởi động lại). Hãy thử lại.")
    try:
        out["undo_available"] = bool(_history(jobs.job_dir(job["id"])))
    except jobs.JobNotFound:
        out["undo_available"] = False
    return out


def _history(d: Path) -> list[Path]:
    return sorted((d / HISTORY_DIR).glob("*.json")) if (d / HISTORY_DIR).is_dir() else []


def _check_editable(job: dict) -> Path:
    d = jobs.job_dir(job["id"])
    if job.get("status") != "done" or not (d / "outline.json").exists():
        raise EditError("Chỉ sửa được bài đã hoàn tất và còn dữ liệu dàn ý.", 409)
    if running(job):
        raise EditError("Đang áp dụng yêu cầu sửa trước, vui lòng đợi.", 409)
    if (job.get("google") or {}).get("state") in ("queued", "running"):
        raise EditError("Đang tạo bản Google Slides, hãy đợi xong rồi sửa.", 409)
    return d


def submit(job_id: str, edits: list[dict]) -> dict:
    """Queue edits ``[{"slide_number": n, "instruction": "..."}]`` (several per slide are merged)."""
    job = jobs.load(job_id)
    d = _check_editable(job)
    pres = Presentation.model_validate_json((d / "outline.json").read_text(encoding="utf-8"))
    merged: dict[int, list[str]] = {}
    for e in edits:
        n = int(e.get("slide_number") or 0)
        text = " ".join(str(e.get("instruction") or "").split())[:MAX_INSTRUCTION]
        if not 1 <= n <= len(pres.slides):
            raise EditError(f"Slide {n} không tồn tại (bài có {len(pres.slides)} slide).")
        if not text:
            raise EditError(f"Yêu cầu sửa slide {n} đang trống.")
        merged.setdefault(n, []).append(text)
    if not merged:
        raise EditError("Chưa có yêu cầu sửa nào.")
    if len(merged) > MAX_EDITS:
        raise EditError(f"Mỗi lần chỉ sửa tối đa {MAX_EDITS} slide.")
    requests = {n: "; ".join(parts) for n, parts in sorted(merged.items())}
    _start(job_id, "edit", sorted(requests), lambda: _run_edit(job_id, requests))
    return {"state": "queued", "slides": sorted(requests)}


def submit_undo(job_id: str) -> dict:
    job = jobs.load(job_id)
    d = _check_editable(job)
    if not _history(d):
        raise EditError("Không còn lần sửa nào để hoàn tác.", 409)
    _start(job_id, "undo", [], lambda: _run_undo(job_id))
    return {"state": "queued"}


def _start(job_id: str, kind: str, slides: list[int], fn) -> None:
    with _lock:
        if job_id in _inflight:
            raise EditError("Đang áp dụng yêu cầu sửa trước, vui lòng đợi.", 409)
        _inflight.add(job_id)
    _set(job_id, state="queued", kind=kind, step=None, slides=slides, error=None, notices=[], message=None)

    def run() -> None:
        try:
            fn()
        except jobs.JobNotFound:
            pass
        except EditError as exc:
            _fail(job_id, str(exc))
        except Exception as exc:
            from .ai.base import AIError
            if isinstance(exc, AIError):
                log.warning("slide edit for job %s failed: %s", job_id, exc)
                _fail(job_id, str(exc))
            else:
                log.exception("slide edit for job %s crashed", job_id)
                _fail(job_id, f"Lỗi không mong muốn: {exc.__class__.__name__}")
        finally:
            with _lock:
                _inflight.discard(job_id)

    _pool.submit(run)


def _set(job_id: str, **fields) -> None:
    with jobs._lock:
        job = jobs.load(job_id)
        jobs.update(job_id, edit={**(job.get("edit") or {}), **fields, "updated_at": time.time()})


def _fail(job_id: str, message: str) -> None:
    try:
        _set(job_id, state="failed", error=message, step=None, message=None)
    except jobs.JobNotFound:
        pass


# ---------------------------------------------------------------------- edit: LLM rewrite of a few slides

def _slide_brief(s: Slide) -> str:
    """Current content of a slide, given to the model as the starting point of the rewrite."""
    data = s.model_dump(include={"title", "subtitle", "layout_type", "key_points", "items", "left_title",
                                 "left_points", "right_title", "right_points", "speaker_notes", "reading_notes"})
    return json.dumps(data, ensure_ascii=False)


def rewrite(task, pres: Presentation, transcript, requests: dict[int, str]) -> dict[int, Slide]:
    """Rewrite the requested slides with the pipeline's slide writer. Returns {slide_number: new Slide}."""
    from .ai.ollama import OutlineEntry, _clock

    p = task.provider
    task._prepare()
    duration = transcript.duration or max((s.end for s in transcript.segments), default=0.0)
    p._req.transcript = transcript
    p._duration = duration
    deck_outline = "\n".join(
        f"{s.slide_id} [{s.layout_type}] {s.title}"
        + (f" ({', '.join(_clock(r.start) + '-' + _clock(r.end) for r in s.source_ranges)})" if s.source_ranges else "")
        for s in pres.slides)
    # Overview slides (no source spans) of a long video use the deck's own notes as source instead of
    # condensing the whole transcript again (that would cost many LLM requests for one edit).
    from .studio import deck_digest
    p._ctx = {"deck_outline": deck_outline, "deck_title": pres.presentation_title, "lang": pres.language,
              "t": transcript, "notes": deck_digest(pres, 300), "with_images": False, "style": "professional"}
    entries = []
    for n, instruction in requests.items():
        s = pres.slides[n - 1]
        entries.append(OutlineEntry(
            slide_id=s.slide_id, number=n, title=s.title, layout_type=s.layout_type,
            purpose=(f"REWRITE this existing slide following the user's edit request: \"{instruction}\". "
                     "Keep everything the request does not ask to change; another layout may be chosen if the "
                     f"request needs it. Current slide: {_slide_brief(s)}"),
            ranges=[{"start": r.start, "end": r.end} for r in s.source_ranges]))
    out: dict[int, Slide] = {}
    size = max(1, settings.ollama_slides_per_batch)
    for i in range(0, len(entries), size):
        batch = entries[i:i + size]
        written = p._write_batch(batch, attempt=0, problems={})
        for e in batch:
            new = written[e.slide_id]
            old = pres.slides[e.number - 1]
            new.image_prompt = old.image_prompt  # the slide keeps its AI illustration, if any
            out[e.number] = new
    return out


def _run_edit(job_id: str, requests: dict[int, str]) -> None:
    from .ai.assist import SideTask

    d = jobs.job_dir(job_id)
    _set(job_id, state="running", step="writing")
    pres, transcript = pipeline.load_outline(d)  # reading notes completed: context for the model
    if transcript is None or not transcript.segments:
        raise EditError("Không còn bản ghi lời nói của bài này nên không sửa được bằng AI.", 409)
    task = SideTask(cache_dir=None, status=lambda m: _set(job_id, message=m))
    new = rewrite(task, pres, transcript, requests)
    stored = _stored_outline(d)  # only the edited slides change in the stored outline
    for n, slide in new.items():
        stored.slides[n - 1] = slide
    notices = sanitize_grounding(stored, transcript.text)
    _save_outline(d, stored)
    _set(job_id, step="building", message=None)
    rebuild(job_id, d)
    _set(job_id, state="done", step=None, notices=notices[:5])


def _run_undo(job_id: str) -> None:
    d = jobs.job_dir(job_id)
    _set(job_id, state="running", step="building")
    history = _history(d)
    if not history:
        raise EditError("Không còn lần sửa nào để hoàn tác.", 409)
    last = history[-1]
    tmp = d / "outline.json.tmp"
    tmp.write_text(last.read_text(encoding="utf-8"), encoding="utf-8")
    tmp.replace(d / "outline.json")
    last.unlink(missing_ok=True)
    rebuild(job_id, d)
    _set(job_id, state="done", step=None, notices=[])


def _stored_outline(d: Path) -> Presentation:
    return Presentation.model_validate_json((d / "outline.json").read_text(encoding="utf-8"))


def _save_outline(d: Path, pres: Presentation) -> None:
    """Keep the previous outline (undo), then replace it atomically."""
    hist = d / HISTORY_DIR
    hist.mkdir(exist_ok=True)
    (hist / f"{time.time_ns()}.json").write_text((d / "outline.json").read_text(encoding="utf-8"), encoding="utf-8")
    for old in _history(d)[:-HISTORY_KEEP]:
        old.unlink(missing_ok=True)
    tmp = d / "outline.json.tmp"
    tmp.write_text(pres.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(d / "outline.json")


# ---------------------------------------------------------------------- rebuild the deck from cached media

def _frames(d: Path) -> list[FrameRef]:
    try:
        meta = json.loads((d / "frames.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [FrameRef(d / "frames" / f["file"], float(f["timestamp"])) for f in meta
            if (d / "frames" / f["file"]).exists()]


def _illustrations(d: Path) -> dict[str, Path]:
    try:
        manifest = json.loads((d / "illustrations" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {sid: d / "illustrations" / f"{sid}.jpg" for sid, e in manifest.items()
            if e.get("state") == "done" and (d / "illustrations" / f"{sid}.jpg").is_file()}


def rebuild(job_id: str, d: Path) -> None:
    """Rebuild the .pptx from outline.json and cached media (validated, atomically replaced), then refresh the
    previews, the library entry and the job result, like the pipeline's last steps."""
    job = jobs.load(job_id)
    opts = job.get("options") or {}
    layout_mode = opts.get("layout_mode") or "presentation"
    pres, _ = pipeline.load_outline(d)
    saved = pres.model_copy(deep=True)  # before illustration layouts are applied (as the pipeline saves it)
    images = _illustrations(d)
    apply_illustrations(pres, set(images))
    tmp = d / "presentation.edit.pptx"
    warnings = build_pptx(pres, tmp, opts.get("style") or "professional", _frames(d),
                          illustrations=images or None, layout_mode=layout_mode)
    report = validate_pptx(tmp, expected_slides=len(pres.slides))
    if not report.ok:
        tmp.unlink(missing_ok=True)
        raise EditError("Tệp PowerPoint dựng lại không hợp lệ: " + "; ".join(report.errors[:3]), 500)
    out = pipeline.output_path(d)
    tmp.replace(out)
    # Previews share one renderer with the pipeline: only render now when no deck is being made, otherwise
    # Reading Mode renders them on demand.
    previews = pipeline._render_previews(d, out) if not jobs.busy() else 0
    pipeline._save_to_library(job_id, d, saved, opts, layout_mode, report.slide_count)
    result = {**(job.get("result") or {}), "slide_count": report.slide_count, "pictures": report.pictures,
              "layouts": [s.layout_type for s in pres.slides], "preview_slides": previews,
              "warnings": (warnings + report.warnings)[:10], "edited_at": time.time()}
    fields: dict = {"result": result}
    google = job.get("google") or {}
    if google.get("meta"):
        # Never overwrite a Google Slides file the user may have edited: mark it out of date.
        fields["google"] = {"state": "stale", "meta": google["meta"], "error": None, "updated_at": time.time()}
    jobs.update(job_id, **fields)
