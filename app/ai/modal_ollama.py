"""Ollama on Modal (GPU): the staged, validated generation of ``OllamaProvider`` with a remote transport.

``ModalOllamaClient`` implements the same small interface as ``OllamaClient`` (version, installed_models,
show, loaded_models, chat) on top of the private worker's proxy-auth HTTPS API
(infrastructure/modal_llm_worker.py). Only timestamped transcript text / transcript chunks are sent (one chat
request at a time, size-limited by the worker); never the video. Health checks read the worker's model marker
and never start a GPU. Credentials (Modal proxy token) stay on the backend: headers only, never logged.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

import httpx

from .. import config
from .base import AIError
from .ollama import OllamaProvider
from .ollama_client import ChatResult, ModelInfo

log = logging.getLogger(__name__)

# Status hook values (the pipeline turns them into Vietnamese messages)
STATUS_GPU_STARTING = "gpu_starting"
STATUS_GENERATING = "generating"


class ModalOllamaClient:
    def __init__(self, base_url: str | None = None, token_id: str | None = None, token_secret: str | None = None,
                 client: httpx.Client | None = None, model: str | None = None):
        s = config.settings
        self.base_url = (base_url or s.modal_llm_worker_url).rstrip("/")
        self._token_id = token_id or s.modal_proxy_token_id
        self._token_secret = token_secret or s.modal_proxy_token_secret
        self.model = model or s.modal_llm_model
        self._client = client
        self._health: dict | None = None
        self.on_status: Callable[[str], None] | None = None
        self.calls: list[dict] = []        # per-request GPU metrics (no content)
        self.cold_starts: list[float] = []
        self.gpu: dict = {}
        self.retries = 0
        if not self.base_url or not self._token_id or not self._token_secret:
            raise AIError("Chưa cấu hình Ollama trên Modal (MODAL_LLM_WORKER_URL, MODAL_PROXY_TOKEN_ID, "
                          "MODAL_PROXY_TOKEN_SECRET). Xem docs/MODAL_CLOUD_SETUP_VI.md.", "modal_not_configured")
        if not self.base_url.startswith("https://"):
            raise AIError("MODAL_LLM_WORKER_URL phải dùng https://.", "modal_not_configured")

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = self._client or httpx.Client(timeout=config.settings.modal_http_timeout_sec, trust_env=False)
        headers = {"Modal-Key": self._token_id, "Modal-Secret": self._token_secret}
        try:
            resp = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise AIError("LLM worker trên Modal phản hồi quá chậm.", "modal_unavailable") from exc
        except httpx.HTTPError as exc:
            raise AIError(f"Không kết nối được LLM worker trên Modal ({exc.__class__.__name__}).",
                          "modal_unavailable") from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code in (401, 403):
            raise AIError("Token truy cập Modal không hợp lệ (kiểm tra MODAL_PROXY_TOKEN_*).", "modal_auth")
        if resp.status_code == 404 and path.startswith("/v1/health"):
            raise AIError("Chưa deploy LLM worker trên Modal hoặc sai MODAL_LLM_WORKER_URL.", "modal_not_deployed")
        if resp.status_code in (400, 413, 422):
            raise AIError(f"LLM worker từ chối yêu cầu: {_detail(resp)}", "modal_invalid_request")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise AIError(f"LLM worker trên Modal tạm thời lỗi (HTTP {resp.status_code}).", "modal_unavailable")
        return resp

    def health(self, refresh: bool = False) -> dict:
        """Deployment and model-file readiness (CPU endpoint; never starts a GPU)."""
        if self._health is None or refresh:
            self._health = self._request("GET", "/v1/health").json() or {}
        return self._health

    # ------------------------------------------------------------------ OllamaClient interface

    def version(self) -> str:
        return str(self.health().get("ollama_version") or "")

    def installed_models(self) -> list[str]:
        h = self.health()
        return [h.get("model")] if h.get("model_ready") and h.get("model") else []

    def show(self, model: str) -> ModelInfo:
        info = self.health().get("model_info") or {}
        return ModelInfo(name=model, capabilities=list(info.get("capabilities") or []),
                         parameter_size=str(info.get("parameter_size") or ""),
                         quantization=str(info.get("quantization") or ""), family=str(info.get("family") or ""),
                         context_length=info.get("context_length"))

    def loaded_models(self) -> list[dict]:
        last = self.calls[-1] if self.calls else {}
        if not last.get("size_mb"):
            return []
        return [{"name": self.model, "size_mb": last.get("size_mb"), "vram_mb": last.get("size_vram_mb"),
                 "context_length": last.get("context_length"), "gpu": last.get("gpu")}]

    def unload(self, model: str) -> None:  # the container scales to zero on its own
        return None

    def warmup(self) -> bool:
        """Best effort: start the GPU container and load the model now (e.g. while Whisper is still running)."""
        try:
            self._request("POST", "/v1/warmup")
            return True
        except AIError as exc:
            log.warning("Modal LLM warm-up failed: %s", exc.code)
            return False

    def chat(self, model: str, messages: list[dict], *, format: dict | str | None = None,
             num_ctx: int | None = None, temperature: float | None = None, num_predict: int | None = None,
             think: bool | None = None, keep_alive: str | None = None,
             check_cancel: Callable[[], None] | None = None, on_tokens: Callable[[int], None] | None = None) -> ChatResult:
        s = config.settings
        options: dict[str, Any] = {"num_ctx": num_ctx or s.ollama_num_ctx,
                                   "temperature": s.ollama_temperature if temperature is None else temperature}
        if num_predict:
            options["num_predict"] = int(num_predict)
        request: dict[str, Any] = {"model": model, "messages": messages, "options": options,
                                   "keep_alive": keep_alive or s.ollama_keep_alive}
        if format is not None:
            request["format"] = format
        if think is not None:
            request["think"] = think
        for attempt in range(2):  # one bounded retry for transient worker failures
            try:
                return self._run(request, check_cancel)
            except _Transient as exc:
                if attempt:
                    raise AIError(str(exc), "modal_unavailable") from None
                self.retries += 1
                log.warning("Modal LLM transient failure (%s); retrying once", exc)
        raise AssertionError("unreachable")

    def _run(self, request: dict, check_cancel: Callable[[], None] | None) -> ChatResult:
        s = config.settings
        body = {"request_id": uuid.uuid4().hex, "request": request}
        started = time.monotonic()
        try:
            call_id = (self._request("POST", "/v1/chat", json=body).json() or {}).get("call_id")
        except AIError as exc:
            if exc.code == "modal_unavailable":
                raise _Transient(str(exc)) from None
            raise
        if not isinstance(call_id, str) or not call_id:
            raise _Transient("LLM worker không trả về mã công việc.")
        announced = False
        while True:
            try:
                if check_cancel:
                    check_cancel()
            except BaseException:
                self._cancel(call_id)
                raise
            elapsed = time.monotonic() - started
            if elapsed > s.modal_llm_timeout_sec:
                self._cancel(call_id)
                raise AIError(f"Ollama trên Modal không trả lời trong {s.modal_llm_timeout_sec} giây "
                              "(MODAL_LLM_REQUEST_TIMEOUT_SECONDS).", "ollama_timeout")
            if not announced and not self.calls and elapsed > 3 and self.on_status:
                announced = True
                self.on_status(STATUS_GPU_STARTING)
            try:
                resp = self._request("GET", f"/v1/result/{call_id}")
            except AIError as exc:
                if exc.code != "modal_unavailable":
                    raise
                time.sleep(s.modal_poll_interval_sec)  # a polling hiccup; the call keeps running remotely
                continue
            data = (resp.json() or {}) if resp.status_code == 200 else {"status": "expired"}
            state = data.get("status")
            if state == "pending":
                time.sleep(s.modal_poll_interval_sec)
                continue
            if state == "done":
                return self._result(data.get("result") or {}, time.monotonic() - started)
            if state == "expired":
                raise _Transient("Kết quả trên LLM worker đã hết hạn.")
            code = str(data.get("error_code") or "worker_error")
            message = str(data.get("error") or "LLM worker báo lỗi.")[:300]
            if data.get("retryable"):
                raise _Transient(message)
            raise AIError(_ERRORS.get(code, f"Ollama trên Modal lỗi: {message}"), f"modal_{code}")

    def _result(self, r: dict, wall: float) -> ChatResult:
        m = dict(r.get("metrics") or {})
        m["round_trip_sec"] = round(wall, 2)
        self.calls.append(m)
        if m.get("cold_start"):
            self.cold_starts.append(round(wall, 2))
        if m.get("gpu"):
            self.gpu = {k: m.get(k) for k in ("gpu", "vram_total_mb", "vram_used_mb", "gpu_fraction", "size_vram_mb")}
        if self.on_status and len(self.calls) == 1:
            self.on_status(STATUS_GENERATING)
        return ChatResult(
            content=str(r.get("content") or ""), thinking=str(r.get("thinking") or ""),
            done_reason=str(r.get("done_reason") or ""), prompt_tokens=int(r.get("prompt_eval_count") or 0),
            output_tokens=int(r.get("eval_count") or 0), load_sec=float(r.get("load_sec") or 0),
            total_sec=float(r.get("total_sec") or 0), eval_sec=float(r.get("eval_sec") or 0),
            prompt_eval_sec=float(r.get("prompt_eval_sec") or 0))

    def _cancel(self, call_id: str) -> None:
        try:
            self._request("POST", f"/v1/cancel/{call_id}")
        except AIError as exc:
            log.warning("cancel of Modal LLM call failed: %s", exc.code)

    def usage(self) -> dict:
        """Measured GPU-side time and a list-price estimate (not the billed amount)."""
        gpu_sec = sum(float(c.get("wall_seconds") or 0) for c in self.calls)
        startup = sum(float(c.get("startup_seconds") or 0) for c in self.calls if c.get("cold_start"))
        return {"gpu_requests": len(self.calls), "gpu_busy_seconds": round(gpu_sec, 1),
                "cold_starts": len(self.cold_starts), "cold_start_round_trip_sec": self.cold_starts[:3],
                "container_startup_seconds": round(startup, 1), "transport_retries": self.retries, **self.gpu,
                "estimated_gpu_cost_usd_excl_idle": round((gpu_sec + startup) * config.settings.modal_gpu_price_per_sec, 4)}


class _Transient(Exception):
    pass


_ERRORS = {
    "model_missing": "Model chưa được tải vào Volume của Modal. Quản trị viên chạy: "
                     "modal run infrastructure/modal_llm_worker.py::setup_model",
    "gpu_not_used": "LLM worker không chạy được model trên GPU (từ chối chạy bằng CPU). Xem log trên Modal.",
    "startup_failed": "LLM worker trên Modal không khởi động được Ollama. Xem log trên Modal.",
    "invalid_request": "LLM worker từ chối yêu cầu (lỗi ứng dụng).",
    "timeout": "Ollama trên Modal xử lý quá thời gian cho phép.",
    "cancelled": "Yêu cầu tới Ollama trên Modal đã bị hủy.",
}


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))[:200]
    except ValueError:
        return ""


class ModalOllamaProvider(OllamaProvider):
    """Ollama (qwen3:4b) on a Modal L4 GPU, with the same staged prompts, validation and checkpoints."""

    name = "modal_ollama"

    def __init__(self, client: ModalOllamaClient | None = None, model: str | None = None):
        model = model or config.settings.modal_llm_model
        super().__init__(client=client or ModalOllamaClient(model=model), model=model)
        self.num_ctx = config.settings.modal_llm_num_ctx

    def _prepare(self) -> None:
        s = config.settings
        health = self.client.health()
        self.server_version = str(health.get("ollama_version") or "")
        if health.get("model") != self.model:
            raise AIError(f"LLM worker trên Modal phục vụ model '{health.get('model')}', khác MODAL_LLM_MODEL="
                          f"'{self.model}'.", "modal_model_mismatch")
        if not health.get("model_ready"):
            raise AIError(_ERRORS["model_missing"], "modal_model_missing")
        self.info = self.client.show(self.model)
        self.think = (s.ollama_think == "on") if self.info.supports_thinking else None
        self.num_ctx = self.clamp_ctx(self.num_ctx)

    def clamp_ctx(self, num_ctx: int) -> int:
        """Never ask for more context than the model or the deployed worker accepts."""
        limit = (self.client.health().get("limits") or {}).get("max_num_ctx")
        if isinstance(limit, int) and limit > 0:
            num_ctx = min(num_ctx, limit)
        if self.info and self.info.context_length:
            num_ctx = min(num_ctx, self.info.context_length)
        return num_ctx

    def describe(self) -> dict:
        return {**super().describe(), "provider": self.name, "location": "modal", "gpu_type": config.settings.modal_llm_gpu,
                "modal": self.client.usage()}
