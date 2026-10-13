"""Gemini behind the small chat interface of ``OllamaClient`` (version, installed_models, show, chat, ...).

Lets the staged, validated generation of ``OllamaProvider`` (transcript chunks -> outline -> slide batches) run on
Gemini for long videos, so each request is a separately validated and checkpointed unit that can fall back to
Ollama on Modal on its own (``app.ai.staged``). JSON schemas are converted to Gemini's ``responseSchema``.
Errors are ``GeminiError`` with the same classification and bounded retries as ``GeminiProvider``.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from ..config import settings
from .gemini import API_URL, GeminiError, _sleep, classify_http_error, extract_text
from .ollama_client import ChatResult, ModelInfo

log = logging.getLogger(__name__)


def to_gemini_schema(schema: Any) -> Any:
    """JSON schema (as used for Ollama ``format``) -> Gemini OpenAPI-subset ``responseSchema``."""
    if isinstance(schema, list):
        return [to_gemini_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            out[k] = v.upper()
        elif k == "properties":
            out[k] = {name: to_gemini_schema(sub) for name, sub in v.items()}
            out["propertyOrdering"] = list(v)
        elif k in ("items", "required", "enum", "minItems", "maxItems", "description", "nullable"):
            out[k] = to_gemini_schema(v)
    return out


class GeminiChatClient:
    provider = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self._client = client
        if not self.api_key:
            raise GeminiError("Thiếu GEMINI_API_KEY.", "missing_key")
        self.requests = 0
        self.retries = 0

    # OllamaClient-compatible metadata (no network)
    def version(self) -> str:
        return "gemini-api"

    def installed_models(self) -> list[str]:
        return [self.model]

    def show(self, model: str) -> ModelInfo:
        return ModelInfo(name=model, capabilities=["completion"], family="gemini", context_length=1_000_000)

    def loaded_models(self) -> list[dict]:
        return []

    def unload(self, model: str) -> None:
        return None

    def chat(self, model: str, messages: list[dict], *, format: dict | str | None = None,
             num_ctx: int | None = None, temperature: float | None = None, num_predict: int | None = None,
             think: bool | None = None, keep_alive: str | None = None,
             check_cancel: Callable[[], None] | None = None, on_tokens: Callable[[int], None] | None = None) -> ChatResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"]
        gen: dict[str, Any] = {"temperature": 0.3 if temperature is None else temperature,
                               "responseMimeType": "application/json",
                               # room for the model's internal reasoning on top of the answer budget
                               "maxOutputTokens": max(8192, 3 * int(num_predict or 0))}
        if isinstance(format, dict):
            gen["responseSchema"] = to_gemini_schema(format)
        body = {"contents": contents, "generationConfig": gen}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        started = time.monotonic()
        deadline = started + settings.ai_timeout_sec * settings.gemini_max_attempts + 30
        failures = 0
        while True:
            if check_cancel:
                check_cancel()
            self.requests += 1
            try:
                data = self._post(model, body, min(settings.ai_timeout_sec, max(5.0, deadline - time.monotonic())))
                text = extract_text(data)
                break
            except GeminiError as exc:
                if not exc.retryable:
                    raise
                failures += 1
                if failures >= settings.gemini_max_attempts:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else 2 * failures + random.random()
                if wait > settings.gemini_max_retry_after_sec or wait > deadline - time.monotonic() - 10:
                    raise
                self.retries += 1
                _sleep(wait, check_cancel)
        cand = (data.get("candidates") or [{}])[0]
        usage = data.get("usageMetadata") or {}
        return ChatResult(content=text, done_reason="length" if cand.get("finishReason") == "MAX_TOKENS" else "stop",
                          prompt_tokens=int(usage.get("promptTokenCount") or 0),
                          output_tokens=int(usage.get("candidatesTokenCount") or 0),
                          total_sec=time.monotonic() - started, eval_sec=time.monotonic() - started)

    def _post(self, model: str, body: dict, timeout: float) -> dict:
        client = self._client or httpx.Client(timeout=timeout)
        try:
            resp = client.post(API_URL.format(model=model), json=body, timeout=timeout,
                               headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"})
        except httpx.TimeoutException as exc:
            raise GeminiError("Gemini phản hồi quá chậm (timeout).", "timeout") from exc
        except httpx.HTTPError as exc:
            raise GeminiError(f"Lỗi kết nối tới Gemini ({exc.__class__.__name__}).", "network") from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code >= 400:
            raise classify_http_error(resp, model)
        try:
            return resp.json()
        except ValueError as exc:
            raise GeminiError("Gemini trả về dữ liệu không đọc được.", "server", resp.status_code) from exc
