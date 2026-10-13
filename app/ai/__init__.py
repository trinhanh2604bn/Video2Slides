"""AI provider registry."""

from __future__ import annotations

from ..config import settings
from .base import AIError, OutlineProvider, OutlineRequest, recommend_slide_count
from .schema import Presentation, Slide

__all__ = ["AIError", "OutlineProvider", "OutlineRequest", "Presentation", "Slide",
           "PROVIDERS", "active_provider_name", "get_provider", "recommend_slide_count"]

# modal_ollama = Ollama on Modal only (default). auto = Gemini first, Ollama on Modal as automatic fallback.
# ollama = local Ollama: kept as an explicit option, never selected automatically.
PROVIDERS = ("auto", "gemini", "modal_ollama", "ollama", "offline")


def active_provider_name(name: str | None = None) -> str:
    return (name or settings.ai_provider or "modal_ollama").strip().lower()


def get_provider(name: str | None = None) -> OutlineProvider:
    name = active_provider_name(name)
    if name == "auto":
        from .router import RoutingProvider
        return RoutingProvider()
    if name == "modal_ollama":
        from .modal_ollama import ModalOllamaProvider
        return ModalOllamaProvider()
    if name == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider()
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider()
    if name == "offline":
        from .offline import OfflineProvider
        return OfflineProvider()
    raise AIError(f"LLM_PROVIDER '{name}' không được hỗ trợ ({', '.join(PROVIDERS)}).")
