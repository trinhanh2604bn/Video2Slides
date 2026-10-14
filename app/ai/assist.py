"""Structured LLM requests outside the deck pipeline: Studio outputs and slide edits.

These requests run on their own background threads (``app.studio``, ``app.slide_edit``), only when the user asks,
never in the job queue. They use the same route as ``LLM_PROVIDER`` and the same machinery as the deck writer
(JSON-schema output, bounded repairs, on-disk cache of validated answers, Gemini -> Modal fallback in ``auto``).

Priority: the Modal LLM worker serves one request at a time, so before every request a side task waits while the
job worker is in an LLM stage (``jobs.main_llm_busy``). A deck being made is therefore never delayed by more than
one side request that was already running when its LLM stage started.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .. import jobs
from ..config import settings
from . import active_provider_name
from .base import AIError
from .ollama import OllamaProvider, _Rejected
from .staged import StagedRoutedProvider

WAIT_POLL_SEC = 2.0
WAIT_MESSAGE = "Đang chờ bài trình chiếu khác dùng xong mô hình AI…"

__all__ = ["AIError", "Rejected", "side_provider", "SideTask"]

Rejected = _Rejected  # raise Rejected([...problems in English...]) from a validator to ask for a repair


def side_provider() -> OllamaProvider:
    """A provider object for small structured requests, following LLM_PROVIDER."""
    name = active_provider_name()
    if name == "offline":
        raise AIError("Tính năng này cần mô hình AI, nhưng LLM_PROVIDER đang là offline.", "offline")
    if name == "ollama":
        return OllamaProvider()
    if name == "modal_ollama":
        from .modal_ollama import ModalOllamaProvider
        return ModalOllamaProvider()
    if name in ("auto", "gemini"):
        from .router import CircuitBreaker, _default_gemini_chat, _default_modal
        gemini = None
        if settings.gemini_api_key and (name == "gemini" or not CircuitBreaker().state()["open"]):
            gemini = _default_gemini_chat()
        if name == "gemini":
            if gemini is None:
                raise AIError("Chưa cấu hình GEMINI_API_KEY.", "missing_key")
            return GeminiOnly(gemini, _default_modal)
        return StagedRoutedProvider(gemini, _default_modal)
    raise AIError(f"LLM_PROVIDER '{name}' không được hỗ trợ.")


class GeminiOnly(StagedRoutedProvider):
    """LLM_PROVIDER=gemini: explicit errors, never a silent switch to another model."""

    def _switch(self, kind: str, exc: Exception) -> None:
        raise exc


class SideTask:
    """One prepared provider used for a few structured requests of a side task."""

    def __init__(self, cache_dir: Path | None, status: Callable[[str | None], None] | None = None,
                 provider: OllamaProvider | None = None):
        self.status = status or (lambda _m: None)
        self.provider = provider or side_provider()
        self.provider._req = SimpleNamespace(check_cancel=None, transcript=None, progress=None, status=None)
        self.provider._cache_dir = cache_dir
        self._prepared = False
        # Every request (including repairs and split batches) passes the priority gate first.
        call = self.provider._call

        def gated_call(*args, **kwargs):
            self.wait_for_main_job()
            return call(*args, **kwargs)

        self.provider._call = gated_call

    @property
    def num_ctx(self) -> int:
        self._prepare()
        return self.provider.num_ctx

    def _prepare(self) -> None:
        if not self._prepared:
            self.wait_for_main_job()  # the health check may start a GPU container too
            self.provider._prepare()
            self._prepared = True

    def wait_for_main_job(self) -> None:
        """Yield to a deck being made: its LLM requests always go first."""
        if not jobs.main_llm_busy():
            return
        self.status(WAIT_MESSAGE)
        while jobs.main_llm_busy():
            time.sleep(WAIT_POLL_SEC)
        self.status(None)

    def ask(self, step: str, build: Callable[[str], list[dict]], schema: dict, num_predict: int,
            validate: Callable[[Any, bool], Any]) -> Any:
        """``build(repair_note) -> messages``; ``validate(data, final)`` returns the value or raises Rejected."""
        self._prepare()
        return self.provider._structured(step, build, schema, num_predict, validate)

    def describe(self) -> dict:
        info = self.provider.describe() if hasattr(self.provider, "describe") else {}
        return {k: info.get(k) for k in ("provider", "model", "requests", "cache_hits", "repairs", "llm_seconds")
                if k in info}
