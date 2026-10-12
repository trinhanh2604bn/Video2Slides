"""Provider interface so other LLM backends (e.g. Groq) can be added later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..transcribe import Transcript
from .schema import Presentation


class AIError(Exception):
    """User-safe AI failure."""


@dataclass
class OutlineRequest:
    transcript: Transcript
    language: str  # "vi", "en" or "auto" (follow the transcript)
    slide_count: int | None  # None = let the model decide
    title_hint: str = ""
    style: str = "professional"
    frame_timestamps: tuple[float, ...] = ()


class OutlineProvider(ABC):
    name: str = "base"
    #: False for degraded, non-LLM providers; surfaced to the user as a warning.
    is_llm: bool = True

    @abstractmethod
    def generate(self, req: OutlineRequest) -> Presentation: ...


def recommend_slide_count(duration_sec: float, word_count: int) -> int:
    """Heuristic: roughly one content slide per ~45-60s of speech, clamped to 6..15."""
    by_time = duration_sec / 50
    by_words = word_count / 130
    est = round(max(by_time, by_words)) + 2  # title + summary
    return max(6, min(15, est))
