"""Local placeholder provider for development and tests (IMAGE_PROVIDER=mock).

It draws a deterministic gradient, not AI art. ``is_real_model = False`` makes the
UI and job result say so explicitly — it must never be presented as an AI image.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import threading

from PIL import Image, ImageDraw

from .base import ImageGenerationProvider, ImageJobStatus, ImageRequest, ImageResult


class MockImageProvider(ImageGenerationProvider):
    name = "mock"
    model_id = "local-placeholder"
    is_real_model = False

    def __init__(self, fail_codes: dict[str, list[str]] | None = None, pending_polls: int = 0):
        # fail_codes: slide_id -> error codes returned by successive attempts (test hook)
        self.fail_codes = {k: list(v) for k, v in (fail_codes or {}).items()}
        self.pending_polls = pending_polls
        self.submitted: list[ImageRequest] = []
        self._jobs: dict[str, dict] = {}
        self._by_key: dict[str, str] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def submit(self, req: ImageRequest, idempotency_key: str | None = None) -> str:
        with self._lock:
            if idempotency_key and idempotency_key in self._by_key:
                return self._by_key[idempotency_key]
            ref = f"mock-{next(self._ids)}"
            codes = self.fail_codes.get(req.slide_id) or []
            fail = codes.pop(0) if codes else None
            self._jobs[ref] = {"req": req, "polls": self.pending_polls, "fail": fail}
            self.submitted.append(req)
            if idempotency_key:
                self._by_key[idempotency_key] = ref
            return ref

    def get_job_status(self, job_ref: str) -> ImageJobStatus:
        with self._lock:
            job = self._jobs.get(job_ref)
            if job is None:
                return ImageJobStatus("expired", error="unknown job", error_code="expired", retryable=True)
            if job["polls"] > 0:
                job["polls"] -= 1
                return ImageJobStatus("pending")
        if job["fail"]:
            code = job["fail"]
            return ImageJobStatus("failed", error=f"simulated {code}", error_code=code,
                                  retryable=code in ("cuda_oom", "timeout", "worker_error"))
        req: ImageRequest = job["req"]
        return ImageJobStatus("done", result=ImageResult(
            data=_gradient(req), format="jpeg", width=req.width, height=req.height, model=self.model_id,
            seed=req.seed, metrics={"inference_seconds": 0.0, "gpu": "none"},
        ))


def _gradient(req: ImageRequest) -> bytes:
    h = hashlib.sha256(f"{req.prompt}:{req.seed}".encode()).digest()
    c1, c2 = tuple(h[0:3]), tuple(h[3:6])
    im = Image.new("RGB", (req.width, req.height), c1)
    draw = ImageDraw.Draw(im)
    for x in range(0, req.width, 8):
        t = x / req.width
        draw.rectangle([x, 0, x + 8, req.height], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    draw.ellipse([req.width * 0.35, req.height * 0.25, req.width * 0.65, req.height * 0.75], fill=tuple(h[6:9]))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()
