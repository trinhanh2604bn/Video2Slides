"""AI provider registry."""

from __future__ import annotations

from ..config import settings
from .base import AIError, OutlineProvider, OutlineRequest, recommend_slide_count
from .schema import Presentation, Slide

__all__ = ["AIError", "OutlineProvider", "OutlineRequest", "Presentation", "Slide",
           "get_provider", "recommend_slide_count"]


def get_provider(name: str | None = None) -> OutlineProvider:
    """Resolve the configured provider. ``auto`` = Gemini when a key exists, else offline."""
    name = (name or settings.ai_provider or "auto").lower()
    if name == "auto":
        name = "gemini" if settings.gemini_api_key else "offline"
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider()
    if name == "offline":
        from .offline import OfflineProvider
        return OfflineProvider()
    raise AIError(f"AI_PROVIDER '{name}' không được hỗ trợ (gemini, offline, auto).")
