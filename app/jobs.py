"""File-backed job store with a single in-process worker thread.

Design notes (single local process; also fits one small cloud instance):
* Job state is persisted as JSON next to its files, so the UI survives page
  reloads and process restarts can be detected.
* This is NOT a durable queue: if the process restarts mid-job the job is
  marked failed and the user can press "retry" (cached steps are reused).
* Exactly one job runs at a time, which bounds CPU/RAM use of Whisper.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import settings

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")

STAGES = [
    "downloading",  # URL imports only
    "queued",
    "extracting_audio",
    "transcribing",
    "analyzing",
    "planning_illustrations",  # only when AI illustrations are enabled for the job
    "generating_images",       # only when AI illustrations are enabled for the job
    "generating",
    "finalizing",
    "completed",
]

_lock = threading.RLock()
_queue: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None
_runner: Callable[[str], None] | None = None


class JobNotFound(Exception):
    pass


class JobCancelled(Exception):
    """Raised inside the pipeline when the user cancelled the job."""


def job_dir(job_id: str) -> Path:
    """Resolve a job directory, rejecting anything that is not a server-issued id."""
    if not JOB_ID_RE.match(job_id or ""):
        raise JobNotFound(job_id)
    path = (settings.jobs_dir / job_id).resolve()
    if path.parent != settings.jobs_dir.resolve():
        raise JobNotFound(job_id)
    return path


def _job_file(job_id: str) -> Path:
    return job_dir(job_id) / "job.json"


def new_job(options: dict[str, Any], filename: str, source: dict[str, Any] | None = None) -> tuple[str, Path]:
    job_id = uuid.uuid4().hex
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=False)
    now = time.time()
    save(job_id, {
        "id": job_id,
        "status": "uploading",
        "stage": "queued",
        "stage_times": {},
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "filename": filename,
        "source": source or {"type": "upload"},
        "options": options,
        "media": None,
        "error": None,
        "result": None,
        "illustrations": None,
    })
    return job_id, d


def load(job_id: str) -> dict[str, Any]:
    path = _job_file(job_id)
    with _lock:
        if not path.is_file():
            raise JobNotFound(job_id)
        return json.loads(path.read_text(encoding="utf-8"))


def save(job_id: str, data: dict[str, Any]) -> None:
    path = _job_file(job_id)
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def update(job_id: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        data = load(job_id)
        data.update(fields)
        save(job_id, data)
        return data


def set_stage(job_id: str, stage: str) -> None:
    with _lock:
        data = load(job_id)
        data["stage"] = stage
        data["stage_times"][stage] = time.time()
        save(job_id, data)


def request_cancel(job_id: str) -> dict[str, Any]:
    """Ask a queued/running job to stop. Queued jobs stop immediately; running jobs at the next check."""
    with _lock:
        data = load(job_id)
        if data.get("status") == "queued":
            data.update(status="cancelled", finished_at=time.time(), error="Đã hủy theo yêu cầu.")
        elif data.get("status") == "running":
            data["cancel_requested"] = True
        save(job_id, data)
        return data


def check_cancel(job_id: str) -> None:
    """Raise JobCancelled when the user asked to cancel this job (cheap; call between steps)."""
    try:
        if load(job_id).get("cancel_requested"):
            raise JobCancelled(job_id)
    except JobNotFound:
        raise JobCancelled(job_id) from None


def find_active_duplicate(content_hash: str, options: dict[str, Any]) -> str | None:
    """An unfinished job for the same video bytes and options (prevents double-submits)."""
    if not settings.jobs_dir.is_dir():
        return None
    for d in settings.jobs_dir.iterdir():
        if not JOB_ID_RE.match(d.name):
            continue
        try:
            data = load(d.name)
        except (JobNotFound, ValueError, OSError):
            continue
        if (data.get("status") in ("queued", "running") and data.get("content_hash") == content_hash
                and data.get("options", {}).get("style") == options.get("style")
                and data.get("options", {}).get("slide_count") == options.get("slide_count")
                and data.get("options", {}).get("illustrations") == options.get("illustrations")):
            return d.name
    return None


def delete(job_id: str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def pending_count() -> int:
    return _queue.qsize()


def enqueue(job_id: str) -> int:
    """Queue a job and return its position (1 = next)."""
    update(job_id, status="queued", stage="queued", error=None, cancel_requested=False, progress=None)
    _queue.put(job_id)
    return _queue.qsize()


def cleanup_expired() -> int:
    """Delete finished/failed jobs older than the TTL."""
    if not settings.jobs_dir.is_dir():
        return 0
    cutoff = time.time() - settings.job_ttl_hours * 3600
    removed = 0
    for d in settings.jobs_dir.iterdir():
        if not d.is_dir() or not JOB_ID_RE.match(d.name):
            continue
        try:
            data = load(d.name)
            created = data.get("created_at", 0)
            active = data.get("status") in ("queued", "running", "downloading")
        except (JobNotFound, ValueError, OSError):
            created, active = d.stat().st_mtime, False
        if created < cutoff and not active:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def recover_after_restart() -> None:
    """Mark jobs interrupted by a restart as failed and re-queue waiting ones."""
    if not settings.jobs_dir.is_dir():
        return
    for d in sorted(settings.jobs_dir.iterdir(), key=lambda p: p.stat().st_mtime):
        if not JOB_ID_RE.match(d.name):
            continue
        try:
            data = load(d.name)
        except (JobNotFound, ValueError, OSError):
            continue
        if data.get("status") in ("running", "uploading", "downloading"):
            update(d.name, status="failed", finished_at=time.time(),
                   error="Máy chủ đã khởi động lại trong khi xử lý. Hãy bấm 'Thử lại'.")
        elif data.get("status") == "queued":
            _queue.put(d.name)


def _loop() -> None:
    last_cleanup = 0.0
    while True:
        try:
            job_id = _queue.get(timeout=60)
        except queue.Empty:
            job_id = None
        if time.time() - last_cleanup > 600:
            last_cleanup = time.time()
            try:
                n = cleanup_expired()
                if n:
                    log.info("Removed %d expired jobs", n)
            except Exception:
                log.exception("cleanup failed")
        if job_id is None or _runner is None:
            continue
        try:
            if load(job_id).get("status") != "queued":  # cancelled or deleted while waiting
                _queue.task_done()
                continue
        except (JobNotFound, ValueError, OSError):
            _queue.task_done()
            continue
        try:
            _runner(job_id)
        except Exception:  # the runner records its own errors; never kill the worker
            log.exception("job %s crashed", job_id)
        finally:
            _queue.task_done()


def start_worker(runner: Callable[[str], None]) -> None:
    global _worker, _runner
    _runner = runner
    if _worker and _worker.is_alive():
        return
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    recover_after_restart()
    _worker = threading.Thread(target=_loop, name="job-worker", daemon=True)
    _worker.start()
