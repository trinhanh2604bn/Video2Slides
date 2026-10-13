"""Readiness of the configured LLM route (used by /api/llm/status). Never returns a secret, never starts a GPU."""

from __future__ import annotations

from ..config import settings
from ..sysinfo import system_memory_mb
from . import active_provider_name
from .base import AIError


def modal_status(client=None) -> dict:
    """Deployment availability vs. model readiness of the Modal LLM worker (CPU health endpoint only)."""
    out: dict = {"provider": "modal_ollama", "model": settings.modal_llm_model, "gpu": settings.modal_llm_gpu,
                 "configured": bool(settings.modal_llm_worker_url and settings.modal_proxy_token_id
                                    and settings.modal_proxy_token_secret),
                 "deployed": False, "model_ready": False, "ready": False}
    if not out["configured"] and client is None:
        out["code"] = "modal_not_configured"
        out["message"] = "Chưa cấu hình MODAL_LLM_WORKER_URL / MODAL_PROXY_TOKEN_*."
        return out
    try:
        from .modal_ollama import ModalOllamaClient
        client = client or ModalOllamaClient()
        h = client.health(refresh=True)
    except AIError as exc:
        out["code"], out["message"] = exc.code, str(exc)
        return out
    info = h.get("model_info") or {}
    out.update(deployed=True, model_ready=bool(h.get("model_ready")), ollama_version=h.get("ollama_version"),
               served_model=h.get("model"), quantization=info.get("quantization"),
               parameter_size=info.get("parameter_size"), limits=h.get("limits"))
    out["ready"] = out["model_ready"] and h.get("model") == settings.modal_llm_model
    out["message"] = ("Ollama trên Modal sẵn sàng (GPU khởi động khi có yêu cầu)." if out["ready"] else
                      "Worker đã deploy nhưng model chưa được tải (chạy setup_model).")
    return out


def gemini_status() -> dict:
    from .router import CircuitBreaker
    ok = bool(settings.gemini_api_key)
    out = {"provider": "gemini", "model": settings.gemini_model, "configured": ok,
           "circuit": CircuitBreaker().state() if ok else None}
    out["message"] = "Đã cấu hình khóa Gemini." if ok else "Chưa có GEMINI_API_KEY (sẽ dùng Ollama trên Modal)."
    return out


def llm_status(timeout: float = 5, provider: str | None = None, client=None) -> dict:
    name = active_provider_name(provider)
    if name == "offline":
        return {"provider": name, "ready": True, "message": "Chế độ offline (không dùng mô hình AI)."}
    if name == "auto":
        g, m = gemini_status(), modal_status()
        gemini_usable = g["configured"] and not (g.get("circuit") or {}).get("open")
        ready = gemini_usable or m["ready"]
        msg = ("Gemini trước, Ollama trên Modal dự phòng." if g["configured"] else "Không có khóa Gemini: dùng Ollama trên Modal.")
        if not m["ready"]:
            msg += f" Dự phòng Modal chưa sẵn sàng: {m.get('message')}"
        return {"provider": "auto", "ready": ready, "gemini": g, "modal_ollama": m, "message": msg}
    if name == "modal_ollama":
        return modal_status(client)
    if name == "gemini":
        g = gemini_status()
        return {**g, "ready": g["configured"],
                "message": g["message"] if g["configured"] else "LLM_PROVIDER=gemini nhưng thiếu GEMINI_API_KEY."}
    if name != "ollama":
        return {"provider": name, "ready": False, "message": f"LLM_PROVIDER '{name}' không được hỗ trợ."}

    from .ollama import _same_model
    from .ollama_client import OllamaClient

    model = settings.ollama_model
    out: dict = {"provider": "ollama", "model": model, "base_url": settings.ollama_base_url, "ready": False,
                 "reachable": False, "model_installed": False, "memory": system_memory_mb()}
    client = client or OllamaClient(timeout=timeout)
    try:
        out["version"] = client.version()
        out["reachable"] = True
        installed = client.installed_models()
        out["model_installed"] = any(_same_model(m, model) for m in installed)
        out["loaded"] = client.loaded_models()
        if out["model_installed"]:
            info = client.show(model)
            out["capabilities"] = info.capabilities
            out["parameter_size"] = info.parameter_size
            out["quantization"] = info.quantization
    except AIError as exc:
        out["code"] = exc.code
        out["message"] = str(exc)
        return out
    others = [m["name"] for m in out.get("loaded") or [] if not _same_model(m["name"], model)]
    if others:
        out["warning"] = (f"Ollama đang giữ thêm model khác trong RAM ({', '.join(others)}); "
                          "chúng sẽ tự giải phóng sau thời gian chờ, hoặc chạy 'ollama stop <tên>'.")
    if not out["model_installed"]:
        out["code"] = "model_missing"
        out["message"] = f"Chưa có model '{model}'. Chạy: ollama pull {model}"
        return out
    out["ready"] = True
    out["message"] = f"Ollama {out['version']} sẵn sàng với model {model}."
    return out
