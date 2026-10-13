"""Minimal client for Ollama's native HTTP API (/api/version, /api/tags, /api/show, /api/ps, /api/chat).

Application settings (OLLAMA_NUM_CTX, OLLAMA_TEMPERATURE, ...) are mapped here to the request
fields Ollama understands (``options.num_ctx``, ``options.temperature``, ``options.num_predict``,
``keep_alive``, ``think``, ``format``). Every failure becomes an ``AIError`` with a stable ``code``
and an actionable Vietnamese message.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import settings
from .base import AIError

log = logging.getLogger(__name__)

# How long to wait for the TCP connection / between streamed chunks (a slow CPU can pause
# for a while during prompt evaluation, so the read timeout is the full request timeout).
CONNECT_TIMEOUT_SEC = 5


@dataclass
class ChatResult:
    content: str
    thinking: str = ""  # reasoning channel, kept apart from the JSON answer and never parsed
    done_reason: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    load_sec: float = 0.0
    total_sec: float = 0.0
    eval_sec: float = 0.0
    prompt_eval_sec: float = 0.0

    @property
    def truncated(self) -> bool:
        return self.done_reason == "length"

    def stats(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens, "output_tokens": self.output_tokens,
            "thinking_chars": len(self.thinking), "done_reason": self.done_reason,
            "load_sec": round(self.load_sec, 2), "prompt_eval_sec": round(self.prompt_eval_sec, 2),
            "eval_sec": round(self.eval_sec, 2), "total_sec": round(self.total_sec, 2),
            "tokens_per_sec": round(self.output_tokens / self.eval_sec, 2) if self.eval_sec else None,
        }


@dataclass
class ModelInfo:
    name: str
    capabilities: list[str] = field(default_factory=list)
    parameter_size: str = ""
    quantization: str = ""
    family: str = ""
    context_length: int | None = None

    @property
    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities


class OllamaClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.timeout = timeout or settings.ollama_timeout_sec
        self._transport = transport

    def _client(self, read_timeout: float | None = None) -> httpx.Client:
        t = httpx.Timeout(connect=CONNECT_TIMEOUT_SEC, read=read_timeout or self.timeout, write=30, pool=5)
        return httpx.Client(base_url=self.base_url, timeout=t, transport=self._transport)

    # ------------------------------------------------------------------ simple endpoints

    def _get(self, path: str, read_timeout: float = 10) -> dict:
        try:
            with self._client(read_timeout) as c:
                resp = c.get(path)
        except httpx.TimeoutException as exc:
            raise AIError(f"Ollama ({self.base_url}) không phản hồi kịp.", "ollama_timeout") from exc
        except httpx.HTTPError as exc:
            raise _unavailable(self.base_url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp, settings.ollama_model)
        return resp.json()

    def version(self) -> str:
        return str(self._get("/api/version").get("version", ""))

    def installed_models(self) -> list[str]:
        return [m.get("name") or m.get("model") for m in self._get("/api/tags").get("models", [])]

    def loaded_models(self) -> list[dict]:
        """Models currently in memory (/api/ps) with their size in MB."""
        out = []
        for m in self._get("/api/ps").get("models", []):
            out.append({"name": m.get("name") or m.get("model"),
                        "size_mb": round((m.get("size") or 0) / 2**20),
                        "vram_mb": round((m.get("size_vram") or 0) / 2**20),
                        "context_length": m.get("context_length")})
        return out

    def show(self, model: str) -> ModelInfo:
        try:
            with self._client(30) as c:
                resp = c.post("/api/show", json={"model": model})
        except httpx.TimeoutException as exc:
            raise AIError(f"Ollama ({self.base_url}) không phản hồi kịp.", "ollama_timeout") from exc
        except httpx.HTTPError as exc:
            raise _unavailable(self.base_url) from exc
        if resp.status_code >= 400:
            raise _http_error(resp, model)
        data = resp.json()
        details = data.get("details") or {}
        ctx = None
        for k, v in (data.get("model_info") or {}).items():
            if k.endswith(".context_length"):
                ctx = int(v)
        return ModelInfo(name=model, capabilities=list(data.get("capabilities") or []),
                         parameter_size=str(details.get("parameter_size") or ""),
                         quantization=str(details.get("quantization_level") or ""),
                         family=str(details.get("family") or ""), context_length=ctx)

    def unload(self, model: str) -> None:
        """Ask Ollama to free the model's memory now (keep_alive=0). Best effort."""
        try:
            with self._client(30) as c:
                c.post("/api/generate", json={"model": model, "keep_alive": 0})
        except httpx.HTTPError:
            pass

    # ------------------------------------------------------------------ chat

    def chat(self, model: str, messages: list[dict], *, format: dict | str | None = None,
             num_ctx: int | None = None, temperature: float | None = None, num_predict: int | None = None,
             think: bool | None = None, keep_alive: str | None = None,
             check_cancel: Callable[[], None] | None = None,
             on_tokens: Callable[[int], None] | None = None) -> ChatResult:
        """One /api/chat request, streamed so a cancellation can stop generation early.

        ``think`` is only sent when not None (older models reject the field).
        ``check_cancel`` may raise; the connection is then closed, which makes Ollama stop generating.
        """
        options: dict[str, Any] = {
            "num_ctx": num_ctx or settings.ollama_num_ctx,
            "temperature": settings.ollama_temperature if temperature is None else temperature,
        }
        if num_predict:
            options["num_predict"] = int(num_predict)
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True, "options": options,
                                "keep_alive": keep_alive or settings.ollama_keep_alive}
        if format is not None:
            body["format"] = format
        if think is not None:
            body["think"] = think

        content: list[str] = []
        thinking: list[str] = []
        final: dict = {}
        started = time.monotonic()
        last_check = 0.0
        chunks = 0
        try:
            with self._client() as c, c.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise _http_error(resp, model)
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        raise _error_from_text(str(msg["error"]), model)
                    m = msg.get("message") or {}
                    if m.get("content"):
                        content.append(m["content"])
                        chunks += 1
                    if m.get("thinking"):
                        thinking.append(m["thinking"])
                    if msg.get("done"):
                        final = msg
                        break
                    now = time.monotonic()
                    if now - started > self.timeout:
                        raise AIError(
                            f"Ollama xử lý quá {self.timeout} giây cho một yêu cầu. Tăng "
                            "OLLAMA_REQUEST_TIMEOUT_SECONDS trong .env, hoặc giảm số slide / OLLAMA_SLIDES_PER_BATCH.",
                            "ollama_timeout")
                    if now - last_check >= 1.0:
                        last_check = now
                        if check_cancel:
                            check_cancel()
                        if on_tokens:
                            on_tokens(chunks)
        except httpx.TimeoutException as exc:
            raise AIError(
                f"Ollama không trả lời trong {self.timeout} giây (máy có thể đang quá tải hoặc thiếu RAM). "
                "Đóng bớt ứng dụng, rồi bấm 'Thử lại'; có thể tăng OLLAMA_REQUEST_TIMEOUT_SECONDS.",
                "ollama_timeout") from exc
        except httpx.HTTPError as exc:
            raise _unavailable(self.base_url) from exc

        if not final:
            raise AIError("Ollama ngắt kết nối giữa chừng (tiến trình model có thể đã bị dừng do thiếu RAM).",
                          "ollama_interrupted")
        ns = 1e9
        return ChatResult(
            content="".join(content), thinking="".join(thinking), done_reason=str(final.get("done_reason") or ""),
            prompt_tokens=int(final.get("prompt_eval_count") or 0), output_tokens=int(final.get("eval_count") or 0),
            load_sec=(final.get("load_duration") or 0) / ns, total_sec=(final.get("total_duration") or 0) / ns,
            eval_sec=(final.get("eval_duration") or 0) / ns,
            prompt_eval_sec=(final.get("prompt_eval_duration") or 0) / ns,
        )


# ---------------------------------------------------------------------- error mapping

def _unavailable(base_url: str) -> AIError:
    return AIError(
        f"Không kết nối được Ollama tại {base_url}. Hãy mở ứng dụng Ollama (hoặc chạy 'ollama serve'), "
        "rồi bấm 'Thử lại'. Cài Ollama tại https://ollama.com/download nếu máy chưa có.",
        "ollama_unavailable")


def _http_error(resp: httpx.Response, model: str) -> AIError:
    try:
        text = str(resp.json().get("error") or resp.text)
    except ValueError:
        text = resp.text
    if resp.status_code == 404:
        return AIError(
            f"Ollama chưa có model '{model}'. Mở cửa sổ lệnh và chạy: ollama pull {model} "
            "(hoặc chạy lại install.bat), rồi bấm 'Thử lại'.", "model_missing")
    return _error_from_text(text, model, resp.status_code)


def _error_from_text(text: str, model: str, status: int | None = None) -> AIError:
    low = text.lower()
    if "not found" in low and ("model" in low or "pull" in low):
        return AIError(f"Ollama chưa có model '{model}'. Chạy: ollama pull {model}", "model_missing")
    if "memory" in low or "out of memory" in low or "insufficient" in low:
        return AIError(
            f"Không đủ RAM để chạy model '{model}' ({text[:160]}). Đóng bớt ứng dụng (trình duyệt nhiều tab, "
            "Chrome, Teams...), hoặc giảm OLLAMA_NUM_CTX, rồi bấm 'Thử lại'.", "insufficient_memory")
    if "context" in low and ("exceed" in low or "length" in low or "too long" in low):
        return AIError("Nội dung gửi tới model vượt quá cửa sổ ngữ cảnh (OLLAMA_NUM_CTX).", "context_overflow")
    if "runner" in low or "terminated" in low or "load" in low or "llama" in low:
        return AIError(
            f"Ollama không nạp được model '{model}' ({text[:160]}). Khởi động lại Ollama; nếu vẫn lỗi, "
            "kiểm tra RAM trống hoặc tải lại model bằng 'ollama pull'.", "model_load_failed")
    return AIError(f"Ollama trả lỗi{f' HTTP {status}' if status else ''}: {text[:300]}", "ollama_error")
