"""Provider interface: the pipeline only talks to ``OutlineProvider`` (Ollama, Gemini, offline, ...)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..transcribe import Transcript
from .schema import Presentation


class AIError(Exception):
    """User-safe AI failure. ``code`` is a stable machine-readable category (e.g. ``ollama_unavailable``)."""

    def __init__(self, message: str, code: str = "ai_error"):
        super().__init__(message)
        self.code = code


@dataclass
class OutlineRequest:
    transcript: Transcript
    language: str  # "vi", "en" or "auto" (follow the transcript)
    slide_count: int | None  # None = let the model decide
    title_hint: str = ""
    style: str = "professional"
    frame_timestamps: tuple[float, ...] = ()
    max_illustrations: int = 0  # > 0: also ask for image_prompt on up to this many slides
    # Optional hooks for long, multi-step providers (Ollama). Others may ignore them.
    cache_dir: Path | None = None  # validated intermediate results (checkpoints) are kept here
    progress: Callable[[str, int, int], None] | None = None  # (step, done, total), real counts only
    check_cancel: Callable[[], None] | None = None  # raises to stop between requests
    status: Callable[[str], None] | None = None  # short Vietnamese status line for the UI (never secrets)


class OutlineProvider(ABC):
    name: str = "base"
    #: False for degraded, non-LLM providers; surfaced to the user as a warning.
    is_llm: bool = True

    @abstractmethod
    def generate(self, req: OutlineRequest) -> Presentation: ...


MAX_SLIDES = 40  # long (60-90 min) videos; one deck stays a summary, not a transcript


def recommend_slide_count(duration_sec: float, word_count: int) -> int:
    """Up to ~10 minutes: about one content slide per 45-60 s of speech (6..15). Longer videos: one more
    slide per ~4 extra minutes, capped at MAX_SLIDES (90 min -> ~36 slides)."""
    by_time = duration_sec / 50
    by_words = word_count / 130
    est = round(max(by_time, by_words)) + 2  # title + summary
    if est <= 15:
        return max(6, est)
    minutes = max(duration_sec, word_count / 2.3) / 60
    return max(15, min(MAX_SLIDES, 15 + round((minutes - 10) / 4)))
