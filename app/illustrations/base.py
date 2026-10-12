"""Provider-independent types for AI illustration generation.

The PPTX engine never talks to a GPU service: it only receives image files keyed
by slide id. Everything GPU-specific lives behind :class:`ImageGenerationProvider`,
so Modal can later be swapped for RunPod, ComfyUI or a dedicated server.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Literal

JobState = Literal["pending", "done", "failed", "expired"]


class ImageGenerationError(Exception):
    """User-safe illustration failure.

    ``retryable`` = a new attempt may succeed (GPU out of memory, timeout, transient
    network error); ``code`` is a stable machine-readable reason.
    """

    def __init__(self, message: str, code: str = "error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ImageRequest:
    presentation_id: str
    slide_id: str
    slide_title: str
    visual_description: str
    prompt: str
    negative_prompt: str
    style: str
    aspect_ratio: str
    width: int
    height: int
    seed: int
    steps: int
    guidance: float
    output_format: str = "jpeg"
    prompt_source: str = "llm"  # llm | fallback

    @property
    def output_id(self) -> str:
        """Content hash: identical requests map to the same image (cache + idempotency key)."""
        return request_key(self.model_params())

    def model_params(self) -> dict:
        """Exactly the parameters that influence the pixels (sent to the worker)."""
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "steps": self.steps,
            "guidance": self.guidance,
            "output_format": self.output_format,
        }

    def to_json(self) -> dict:
        return {**asdict(self), "output_id": self.output_id}

    @classmethod
    def from_json(cls, data: dict) -> "ImageRequest":
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in names})


def request_key(params: dict, model_id: str = "") -> str:
    payload = json.dumps({**params, "model": model_id}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class ImageResult:
    data: bytes
    format: str
    width: int
    height: int
    model: str
    seed: int
    metrics: dict = field(default_factory=dict)  # inference_seconds, peak_vram_mb, gpu, ...


@dataclass
class ImageJobStatus:
    state: JobState
    result: ImageResult | None = None
    error: str = ""
    error_code: str = ""
    retryable: bool = False
    checked_at: float = field(default_factory=time.time)


class ImageGenerationProvider(ABC):
    """Interface every GPU backend implements.

    Long-running work is split into submit (returns a provider job reference that
    is persisted in the job folder) and polling, so the web server never blocks on
    GPU inference and a restarted backend can resume polling instead of paying for
    the same image twice.
    """

    name: str = "base"
    model_id: str = ""
    #: False for the local placeholder provider; surfaced to users so nothing pretends to be AI art.
    is_real_model: bool = True

    @abstractmethod
    def submit(self, req: ImageRequest, idempotency_key: str | None = None) -> str:
        """Queue one image and return the provider's job reference.

        Submitting twice with the same ``idempotency_key`` must not generate twice
        (a timed-out submit can then be repeated safely).
        """

    @abstractmethod
    def get_job_status(self, job_ref: str) -> ImageJobStatus:
        """Non-blocking status check. Raises ImageGenerationError on transport problems."""

    def generate_batch(self, reqs: list[ImageRequest]) -> list[str]:
        """Queue several images (the worker runs them sequentially on one warm GPU)."""
        return [self.submit(r) for r in reqs]

    def retrieve_image(self, job_ref: str, timeout: float, poll_interval: float = 3.0) -> ImageResult:
        """Block until ``job_ref`` finishes (used by scripts and single-image calls)."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_job_status(job_ref)
            if status.state == "done" and status.result is not None:
                return status.result
            if status.state in ("failed", "expired"):
                raise ImageGenerationError(status.error or "Tạo ảnh thất bại.", status.error_code or status.state,
                                           status.retryable)
            if time.monotonic() >= deadline:
                raise ImageGenerationError("Quá thời gian chờ tạo ảnh.", "timeout", retryable=True)
            time.sleep(poll_interval)

    def generate_image(self, req: ImageRequest, timeout: float, poll_interval: float = 3.0) -> ImageResult:
        return self.retrieve_image(self.submit(req), timeout, poll_interval)

    def handle_failure(self, status: ImageJobStatus, attempt: int, max_attempts: int) -> bool:
        """Return True when the failed image should be submitted again."""
        return status.retryable and attempt < max_attempts

    def cancel(self, job_ref: str) -> None:  # optional
        return None

    def health(self) -> dict:
        return {"ok": True, "provider": self.name}
