"""LLM provider router for ``LLM_PROVIDER=auto``: Gemini first, Ollama on Modal (GPU) as automatic fallback.

Bounded state machine for one generation job (the content-generation unit for Gemini is the whole deck, which
Gemini writes in one validated answer; Ollama on Modal writes it in checkpointed stages):

    no GEMINI_API_KEY ------------------------------------------------> Modal Ollama (no Gemini request)
    circuit open / job already fell back earlier (same key + model) ---> Modal Ollama (no Gemini request)
    Gemini (<= GEMINI_MAX_ATTEMPTS requests + <= LLM_MAX_REPAIR_ATTEMPTS repairs, one time budget)
        ok                                 -> done (circuit reset)
        auth | config | quota              -> Modal Ollama at once (circuit opens at once)
        rate_limit | server | network | timeout (after bounded retries) -> Modal Ollama
        invalid_output (after bounded repairs)                          -> Modal Ollama
        safety refusal                     -> STOP (never bypassed with another provider)
        bad_request (our own bug)          -> STOP (reported, not hidden by a fallback)
        user cancellation                  -> STOP (JobCancelled propagates, nothing else runs)
    Modal Ollama fails                     -> STOP with a Vietnamese error listing both providers' safe statuses;
                                              transcript and validated LLM checkpoints stay in the job folder.

After an eligible Gemini failure the job is pinned to Modal Ollama (``llm_route.json`` in the job folder), so a
retry of that job does not hit the unavailable service again. The cross-job circuit breaker lives in
``DATA_DIR/llm_circuit.json`` and is keyed by a hash of (key, model): a new key or model re-evaluates it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

from ..config import settings
from .base import AIError, OutlineProvider, OutlineRequest
from .schema import Presentation

log = logging.getLogger(__name__)

FALLBACK_KINDS = {"missing_key", "auth", "config", "quota", "rate_limit", "server", "network", "timeout",
                  "invalid_output"}
OPEN_AT_ONCE = {"auth", "config", "quota"}
REASONS_VI = {
    "missing_key": "chưa cấu hình GEMINI_API_KEY",
    "auth": "khóa Gemini không hợp lệ hoặc không có quyền",
    "config": "model/endpoint Gemini không khả dụng (kiểm tra GEMINI_MODEL)",
    "quota": "Gemini đã hết hạn mức (quota)",
    "rate_limit": "Gemini đang giới hạn tốc độ",
    "server": "Gemini tạm thời lỗi máy chủ",
    "network": "không kết nối được Gemini",
    "timeout": "Gemini phản hồi quá chậm",
    "invalid_output": "Gemini chưa tạo được nội dung hợp lệ sau số lần sửa cho phép",
    "circuit_open": "Gemini vừa lỗi liên tiếp nên tạm thời được bỏ qua",
    "sticky": "công việc này đã chuyển sang Ollama trên Modal ở lần chạy trước",
}

MSG_GEMINI = "Đang phân tích nội dung bằng Gemini."
MSG_FALLBACK = "Gemini không khả dụng, đang chuyển sang Ollama trên Modal."
MSG_MODAL = "Đang dùng Ollama trên Modal (không có khóa Gemini)."
MSG_GPU = "Đang khởi động mô hình trên GPU."
MSG_QWEN = "Đang tạo nội dung slide bằng Qwen3."


def gemini_fingerprint() -> str:
    """Identifies the Gemini configuration without storing the key."""
    raw = f"{settings.gemini_api_key}|{settings.gemini_model}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class CircuitBreaker:
    _lock = threading.Lock()

    def __init__(self, path: Path | None = None):
        self.path = path or settings.data_dir / "llm_circuit.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if data.get("fingerprint") != gemini_fingerprint():
            return {}  # credentials / model changed: re-evaluate from scratch
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**data, "fingerprint": gemini_fingerprint()}), encoding="utf-8")
        tmp.replace(self.path)

    def state(self) -> dict:
        with self._lock:
            data = self._load()
        open_until = float(data.get("open_until") or 0)
        return {"open": open_until > time.time(), "open_until": open_until or None,
                "consecutive_failures": int(data.get("failures") or 0), "last_reason": data.get("reason")}

    def record_failure(self, kind: str) -> dict:
        with self._lock:
            data = self._load()
            failures = int(data.get("failures") or 0) + 1
            data.update(failures=failures, reason=kind, last_failure_at=time.time())
            if kind in OPEN_AT_ONCE or failures >= settings.llm_circuit_failure_threshold:
                data["open_until"] = time.time() + settings.llm_circuit_cooldown_sec
            self._save(data)
        return self.state()

    def record_success(self) -> None:
        with self._lock:
            if self.path.exists():
                self._save({})


class RoutingProvider(OutlineProvider):
    name = "auto"

    def __init__(self, circuit: CircuitBreaker | None = None, gemini_factory=None, modal_factory=None,
                 gemini_chat_factory=None):
        self.circuit = circuit or CircuitBreaker()
        self._gemini_factory = gemini_factory or _default_gemini
        self._modal_factory = modal_factory or _default_modal
        self._gemini_chat_factory = gemini_chat_factory or _default_gemini_chat
        self.used_provider: str | None = None
        self.used_model: str | None = None
        self.route: dict = {}
        self._inner: dict[str, OutlineProvider] = {}

    # ------------------------------------------------------------------ public

    def generate(self, req: OutlineRequest) -> Presentation:
        started = time.monotonic()
        self.route = {"mode": "auto", "attempted": [], "fallback_reason": None, "fallback_reason_text": None,
                      "warnings": [], "timings": {}, "providers_used": []}
        say = req.status or (lambda _m: None)
        skip = self._skip_reason(req)
        if _is_long(req):
            # Long videos always use the staged route (also when Gemini is skipped), so validated stages are
            # reused on 'Thử lại' whichever model wrote them.
            return self._generate_staged(req, say, started, skip)
        if skip is None:
            say(MSG_GEMINI)
            t0 = time.monotonic()
            self.route["attempted"].append("gemini")
            try:
                gemini = self._gemini_factory()
                self._inner["gemini"] = gemini
                pres = gemini.generate(req)
            except AIError as exc:
                self.route["timings"]["gemini_sec"] = round(time.monotonic() - t0, 2)
                kind = getattr(exc, "kind", None) or "server"
                self.route["gemini_error"] = {"kind": kind, "message": str(exc)[:200]}
                if kind not in FALLBACK_KINDS:
                    # Safety refusal or our own bad request: report, never bypass with another provider.
                    raise
                skip = kind
                if kind != "missing_key":
                    self.route["circuit"] = self.circuit.record_failure(kind)
                    _write_sticky(req, kind)
                if kind == "config":
                    self.route["warnings"].append(f"Cấu hình Gemini cần kiểm tra: {exc}")
                say(MSG_FALLBACK)
            else:
                self.route["timings"]["gemini_sec"] = round(time.monotonic() - t0, 2)
                self.circuit.record_success()
                return self._done(req, pres, "gemini", gemini, started)
        elif skip == "missing_key":
            say(MSG_MODAL)
        else:
            say(MSG_FALLBACK)

        self.route["fallback_reason"] = skip
        self.route["fallback_reason_text"] = REASONS_VI.get(skip, skip)
        self.route["attempted"].append("modal_ollama")
        t0 = time.monotonic()
        try:
            modal = self._modal_factory()
            self._inner["modal_ollama"] = modal
            client = getattr(modal, "client", None)
            if client is not None and hasattr(client, "on_status"):
                client.on_status = lambda st: say(MSG_GPU if st == "gpu_starting" else MSG_QWEN)
            pres = modal.generate(req)
        except AIError as exc:
            self.route["timings"]["modal_ollama_sec"] = round(time.monotonic() - t0, 2)
            self.route["modal_error"] = {"code": exc.code, "message": str(exc)[:200]}
            gem = REASONS_VI.get(skip, skip)
            raise AIError(
                f"Không tạo được nội dung slide bằng cả hai mô hình. Gemini: {gem}. Ollama trên Modal: {exc} "
                "Bản ghi lời nói và các phần đã hoàn thành được giữ lại; bấm 'Thử lại' sau khi khắc phục.",
                "all_providers_failed") from exc
        self.route["timings"]["modal_ollama_sec"] = round(time.monotonic() - t0, 2)
        return self._done(req, pres, "modal_ollama", modal, started)

    def _generate_staged(self, req: OutlineRequest, say, started: float, skip: str | None = None) -> Presentation:
        """Long transcript: validated stages, Gemini first (unless skipped), each falling back to Modal on its own."""
        from .staged import StagedRoutedProvider

        self.route["mode"] = "staged"
        if skip is None:
            self.route["attempted"].append("gemini")
        else:
            self.route["attempted"].append("modal_ollama")
            self.route["fallback_reason"] = skip
            self.route["fallback_reason_text"] = REASONS_VI.get(skip, skip)
            say(MSG_MODAL if skip == "missing_key" else MSG_FALLBACK)

        def modal_factory():
            modal = self._modal_factory()
            client = getattr(modal, "client", None)
            if client is not None and hasattr(client, "on_status"):
                client.on_status = lambda st: say(MSG_GPU if st == "gpu_starting" else MSG_QWEN)
            self._inner["modal_ollama"] = modal
            return modal

        def on_fallback(kind: str, exc: Exception) -> None:
            self.route["attempted"].append("modal_ollama")
            self.route["gemini_error"] = {"kind": kind, "message": str(exc)[:200]}
            self.route["fallback_reason"] = kind
            self.route["fallback_reason_text"] = REASONS_VI.get(kind, kind) + " (từ phần đang làm trở đi)"
            self.route["circuit"] = self.circuit.record_failure(kind)
            _write_sticky(req, kind)
            say(MSG_FALLBACK)

        gemini_client = self._gemini_chat_factory() if skip is None else None
        staged = StagedRoutedProvider(gemini_client, modal_factory, on_fallback=on_fallback, say=say)
        self._inner["staged"] = staged
        try:
            pres = staged.generate(req)
        except AIError as exc:
            self.route["providers_used"] = staged.providers_used
            if getattr(exc, "kind", None) is not None or (staged.fallback is None and skip is None):
                raise  # Gemini-only failure that must not fall back (safety, bad request)
            reason = staged.fallback["kind"] if staged.fallback else skip
            gem = REASONS_VI.get(reason, reason)
            raise AIError(
                f"Không tạo được nội dung slide bằng cả hai mô hình. Gemini: {gem}. Ollama trên Modal: {exc} "
                "Bản ghi lời nói và các phần đã hoàn thành được giữ lại; bấm 'Thử lại' sau khi khắc phục.",
                "all_providers_failed") from exc
        if staged.fallback is None and "gemini" in staged.providers_used:
            self.circuit.record_success()
        self.route["timings"]["staged_sec"] = round(time.monotonic() - started, 2)
        provider = staged.providers_used[-1] if staged.providers_used else staged.active
        self._done(req, pres, provider, staged, started)
        self.route["providers_used"] = staged.providers_used or [provider]
        self.route["model"] = self.used_model = staged.model
        return pres

    def describe(self) -> dict:
        if "staged" in self._inner:
            return {**self._inner["staged"].describe(), "mode": "auto", "route": self.route}
        inner = self._inner.get(self.used_provider or "") or self._inner.get("modal_ollama") \
            or self._inner.get("gemini")
        base = inner.describe() if inner is not None and hasattr(inner, "describe") else {}
        out = {**base, "provider": self.used_provider or base.get("provider"), "mode": "auto", "route": self.route}
        if "gemini" in self._inner and self.used_provider != "gemini":
            out["gemini_attempts"] = self._inner["gemini"].describe() if hasattr(self._inner["gemini"], "describe") else {}
        return out

    def will_use_modal(self, cache_dir: Path | None) -> bool:
        """True when this job will certainly skip Gemini (no key, pinned job, open circuit) - used to pre-warm
        the Modal GPU early. Never probes Gemini."""
        req = OutlineRequest(transcript=None, language="", slide_count=None, cache_dir=cache_dir)  # type: ignore[arg-type]
        return self._skip_reason(req) is not None

    # ------------------------------------------------------------------ helpers

    def _skip_reason(self, req: OutlineRequest) -> str | None:
        if not settings.gemini_api_key:
            return "missing_key"
        if _read_sticky(req):
            return "sticky"
        st = self.circuit.state()
        if st["open"]:
            self.route["circuit"] = st
            return "circuit_open"
        return None

    def _done(self, req: OutlineRequest, pres: Presentation, provider: str, inner, started: float) -> Presentation:
        self.used_provider = provider
        self.used_model = getattr(inner, "used_model", None) or getattr(inner, "model", None)
        self.route["successful_provider"] = provider
        self.route["model"] = self.used_model
        self.route["providers_used"] = [provider]  # every slide of this deck comes from this provider
        self.route["timings"]["total_sec"] = round(time.monotonic() - started, 2)
        log.info("LLM route: attempted=%s used=%s reason=%s", self.route["attempted"], provider,
                 self.route.get("fallback_reason"))
        return pres


def _is_long(req: OutlineRequest) -> bool:
    t = req.transcript
    duration = (t.duration or (t.segments[-1].end if t.segments else 0)) if t is not None else 0
    return duration >= settings.llm_staged_min_sec


def _default_gemini_chat():
    from .gemini_chat import GeminiChatClient
    return GeminiChatClient()


def _default_gemini():
    from .gemini import GeminiProvider
    return GeminiProvider()


def _default_modal():
    from .modal_ollama import ModalOllamaProvider
    return ModalOllamaProvider()


def _sticky_file(req: OutlineRequest) -> Path | None:
    return (req.cache_dir / "llm_route.json") if req.cache_dir else None


def _write_sticky(req: OutlineRequest, kind: str) -> None:
    f = _sticky_file(req)
    if f is None:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"provider": "modal_ollama", "reason": kind, "fingerprint": gemini_fingerprint(),
                             "at": time.time()}), encoding="utf-8")


def _read_sticky(req: OutlineRequest) -> bool:
    f = _sticky_file(req)
    if f is None or not f.is_file():
        return False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("fingerprint") == gemini_fingerprint()  # a new key/model gives Gemini another chance
