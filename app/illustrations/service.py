"""Orchestrates one presentation's illustration batch: cache -> budget -> submit -> poll -> validate.

Idempotency and durability:
* Every request has a content key (prompt, size, steps, guidance, seed, model).
  Finished images are cached under that key, so retries, refreshes and
  re-generated decks never pay twice for the same picture.
* The provider job reference is written to ``illustrations/manifest.json`` in the
  job folder right after submit. After a backend restart the retried job resumes
  polling those references instead of submitting again.
* Submits carry an idempotency key (content key + attempt number); the worker
  de-duplicates on it, so a submit that timed out can be repeated safely.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config
from .base import ImageGenerationError, ImageGenerationProvider, ImageJobStatus, ImageRequest, request_key
from .planner import IllustrationPlan
from .usage import record_gpu_seconds, reserve
from .validation import sanitize_image

log = logging.getLogger(__name__)

MAX_TRANSPORT_ERRORS = 10  # consecutive failed polls for one image before giving up on it
_manifest_lock = threading.RLock()


def get_image_provider(name: str | None = None) -> ImageGenerationProvider:
    name = (name or config.settings.image_provider or "modal_sdxl").lower()
    if name == "modal_sdxl":
        from .modal_sdxl import ModalSDXLImageProvider
        return ModalSDXLImageProvider()
    if name == "mock":
        from .mock import MockImageProvider
        return MockImageProvider()
    raise ImageGenerationError(f"IMAGE_PROVIDER '{name}' không được hỗ trợ (modal_sdxl, mock).", "not_configured")


@dataclass
class IllustrationOutcome:
    images: dict[str, Path] = field(default_factory=dict)  # slide_id -> sanitized JPEG
    requested: int = 0
    failed: list[dict] = field(default_factory=list)       # {slide_id, code, message}
    skipped: list[str] = field(default_factory=list)
    from_cache: int = 0
    est_cost_usd: float = 0.0
    gpu_seconds: float = 0.0
    provider: str = ""
    real_model: bool = True

    def summary(self) -> dict:
        return {
            "requested": self.requested,
            "generated": len(self.images),
            "from_cache": self.from_cache,
            "failed": self.failed[:8],
            "skipped": self.skipped[:8],
            "provider": self.provider,
            "real_model": self.real_model,
            "est_cost_usd": round(self.est_cost_usd, 4),
            "gpu_seconds": round(self.gpu_seconds, 1),
        }


def cache_dir() -> Path:
    return config.settings.illustrations_dir / "cache"


def prune_cache() -> int:
    d = cache_dir()
    if not d.is_dir():
        return 0
    cutoff = time.time() - config.settings.image_cache_ttl_hours * 3600
    removed = 0
    for f in d.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


class IllustrationService:
    def __init__(self, job_dir: Path, provider: ImageGenerationProvider,
                 progress: Callable[[dict], None] | None = None, sleep: Callable[[float], None] = time.sleep,
                 should_cancel: Callable[[], bool] | None = None):
        self.dir = job_dir / "illustrations"
        self.provider = provider
        self.progress = progress or (lambda _: None)
        self.should_cancel = should_cancel or (lambda: False)
        self.sleep = sleep
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ manifest
    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    def load_manifest(self) -> dict[str, dict]:
        with _manifest_lock:
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

    def _save(self, manifest: dict[str, dict]) -> None:
        with _manifest_lock:
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.manifest_path)

    def key_for(self, req: ImageRequest) -> str:
        return request_key(req.model_params(), self.provider.model_id)

    # ------------------------------------------------------------------ main entry
    def run(self, plan: IllustrationPlan) -> IllustrationOutcome:
        s = config.settings
        out = IllustrationOutcome(requested=len(plan.requests), skipped=list(plan.skipped),
                                  provider=self.provider.name, real_model=self.provider.is_real_model)
        prune_cache()
        manifest = self.load_manifest()
        wanted = {r.slide_id for r in plan.requests}
        for stale in [k for k in manifest if k not in wanted]:
            manifest.pop(stale)

        to_submit: list[ImageRequest] = []
        pending: dict[str, ImageRequest] = {}
        for req in plan.requests:
            key = self.key_for(req)
            entry = manifest.get(req.slide_id)
            if not entry or entry.get("key") != key:
                entry = manifest[req.slide_id] = {"key": key, "request": req.to_json(), "state": "planned",
                                                  "attempts": 0}
            local = self.dir / f"{req.slide_id}.jpg"
            cached = cache_dir() / f"{key}.jpg"
            if entry["state"] == "done" and local.is_file():
                out.images[req.slide_id] = local
            elif cached.is_file():
                shutil.copyfile(cached, local)
                entry.update(state="done", source="cache", file=local.name)
                out.images[req.slide_id] = local
                out.from_cache += 1
            elif entry["state"] == "submitted" and entry.get("job_ref"):
                pending[req.slide_id] = req  # resume polling after a restart / retry
            else:
                to_submit.append(req)
        self._save(manifest)
        self._report(out, manifest)

        if to_submit:
            try:
                out.est_cost_usd += reserve(len(to_submit))
            except ImageGenerationError as exc:
                for req in to_submit:
                    self._fail(manifest, out, req, exc.code, str(exc))
                to_submit = []
            for req in to_submit:
                if self._submit(manifest, out, req):
                    pending[req.slide_id] = req
            self._save(manifest)

        deadline = time.monotonic() + s.image_batch_timeout_sec
        errors: dict[str, int] = {}
        while pending:
            if self.should_cancel():
                # Stop paying for work nobody will use; finished images stay cached for a retry.
                for sid in list(pending):
                    self.provider.cancel(manifest[sid]["job_ref"])
                    manifest[sid].update(state="planned", job_ref=None)
                self._save(manifest)
                raise ImageGenerationError("Đã hủy theo yêu cầu.", "cancelled")
            if time.monotonic() >= deadline:
                for sid, req in list(pending.items()):
                    self.provider.cancel(manifest[sid]["job_ref"])  # stop paying for abandoned work
                    self._fail(manifest, out, req, "timeout", "Quá thời gian chờ GPU tạo ảnh.")
                pending.clear()
                break
            for sid, req in list(pending.items()):
                entry = manifest[sid]
                try:
                    status = self.provider.get_job_status(entry["job_ref"])
                    errors[sid] = 0
                except ImageGenerationError as exc:
                    if not exc.retryable:
                        self._fail(manifest, out, req, exc.code, str(exc))
                        pending.pop(sid)
                        continue
                    errors[sid] = errors.get(sid, 0) + 1
                    if errors[sid] >= MAX_TRANSPORT_ERRORS:
                        self._fail(manifest, out, req, exc.code, str(exc))
                        pending.pop(sid)
                    continue

                if status.state == "pending":
                    continue
                if status.state == "done" and status.result is not None:
                    try:
                        self._store(manifest, out, req, status.result)
                        pending.pop(sid)
                        continue
                    except ImageGenerationError as exc:
                        status = ImageJobStatus("failed", error=str(exc), error_code=exc.code, retryable=exc.retryable)
                # failed / expired / invalid image
                if self.provider.handle_failure(status, entry["attempts"], s.image_max_attempts):
                    log.info("retrying illustration %s after %s", sid, status.error_code)
                    try:
                        out.est_cost_usd += reserve(1)
                    except ImageGenerationError as exc:
                        self._fail(manifest, out, req, exc.code, str(exc))
                        pending.pop(sid)
                        continue
                    if not self._submit(manifest, out, req):
                        pending.pop(sid)
                else:
                    self._fail(manifest, out, req, status.error_code or status.state,
                               status.error or "Tạo ảnh thất bại.")
                    pending.pop(sid)
            self._save(manifest)
            self._report(out, manifest)
            if pending:
                self.sleep(s.image_poll_interval_sec)

        self._save(manifest)
        self._report(out, manifest)
        return out

    # ------------------------------------------------------------------ steps
    def _submit(self, manifest: dict, out: IllustrationOutcome, req: ImageRequest) -> bool:
        entry = manifest[req.slide_id]
        attempt = entry.get("attempts", 0) + 1
        idem = f"{entry['key']}-a{attempt}"
        last: ImageGenerationError | None = None
        for i in range(3):  # transport retries reuse the same idempotency key -> no duplicate GPU work
            try:
                ref = self.provider.submit(req, idempotency_key=idem)
                entry.update(state="submitted", job_ref=ref, attempts=attempt, submitted_at=time.time(),
                             error=None, error_code=None)
                self._save(manifest)
                return True
            except ImageGenerationError as exc:
                last = exc
                if not exc.retryable:
                    break
                self.sleep(min(10.0, 2.0 * (i + 1)))
        entry["attempts"] = attempt
        self._fail(manifest, out, req, last.code if last else "error", str(last) if last else "Gửi yêu cầu thất bại.")
        return False

    def _store(self, manifest: dict, out: IllustrationOutcome, req: ImageRequest, result) -> None:
        data, w, h = sanitize_image(result.data, req.width, req.height)
        local = self.dir / f"{req.slide_id}.jpg"
        tmp = local.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(local)
        entry = manifest[req.slide_id]
        cache = cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, cache / f"{entry['key']}.jpg")
        secs = float(result.metrics.get("inference_seconds") or 0.0)
        if self.provider.is_real_model:
            record_gpu_seconds(secs)
        out.gpu_seconds += secs
        entry.update(state="done", source="gpu", file=local.name, width=w, height=h, model=result.model,
                     metrics=result.metrics, finished_at=time.time())
        out.images[req.slide_id] = local

    def _fail(self, manifest: dict, out: IllustrationOutcome, req: ImageRequest, code: str, message: str) -> None:
        entry = manifest.setdefault(req.slide_id, {"key": self.key_for(req), "request": req.to_json(), "attempts": 0})
        entry.update(state="failed", error_code=code, error=message[:300])
        if not any(f["slide_id"] == req.slide_id for f in out.failed):
            out.failed.append({"slide_id": req.slide_id, "code": code, "message": message[:300]})
        log.warning("illustration %s failed: %s", req.slide_id, code)

    def _report(self, out: IllustrationOutcome, manifest: dict) -> None:
        states = [e.get("state") for e in manifest.values()]
        self.progress({
            "total": out.requested,
            "done": states.count("done"),
            "failed": states.count("failed"),
            "in_progress": states.count("submitted"),
        })
