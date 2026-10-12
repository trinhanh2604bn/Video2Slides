"""Google Gemini provider using the public REST API (no extra SDK dependency)."""

from __future__ import annotations

import json
import logging
import random
import time

import httpx
from pydantic import ValidationError

from ..config import settings
from .base import AIError, OutlineProvider, OutlineRequest
from .prompt import SYSTEM_PROMPT, build_repair_prompt, build_user_prompt
from .schema import Presentation, gemini_response_schema

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(OutlineProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None,
                 fallback_models: tuple[str, ...] | None = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        # Used in order when the current model is overloaded (HTTP 503) instead of failing the job.
        fallbacks = settings.gemini_fallback_models if fallback_models is None else fallback_models
        self.fallback_models = tuple(m for m in fallbacks if m and m != self.model)
        self.used_model = self.model
        if not self.api_key:
            raise AIError("Thiếu GEMINI_API_KEY. Hãy thêm khóa vào tệp .env (hoặc Secrets của máy chủ đám mây).")
        self._client = client

    def _post(self, contents: list[dict], with_image_prompt: bool = False, model: str | None = None) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                "responseSchema": gemini_response_schema(include_image_prompt=with_image_prompt),
            },
        }
        client = self._client or httpx.Client(timeout=settings.ai_timeout_sec)
        try:
            # Key goes in a header, never in the URL, so it can't leak into logs.
            resp = client.post(
                API_URL.format(model=model or self.model),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=body,
            )
        except httpx.TimeoutException as exc:
            raise _Retryable("Gemini phản hồi quá chậm (timeout).") from exc
        except httpx.HTTPError as exc:
            raise _Retryable(f"Lỗi kết nối tới Gemini: {exc.__class__.__name__}") from exc
        finally:
            if self._client is None:
                client.close()

        if resp.status_code in (429, 500, 502, 503, 504):
            raise _Retryable(f"Gemini tạm thời không khả dụng (HTTP {resp.status_code}).", resp.status_code)
        if resp.status_code in (401, 403):
            raise AIError("GEMINI_API_KEY không hợp lệ hoặc không có quyền truy cập model.")
        if resp.status_code == 404:
            raise AIError(f"Model Gemini '{model or self.model}' không tồn tại hoặc không còn dùng được. "
                          "Kiểm tra biến GEMINI_MODEL / GEMINI_FALLBACK_MODELS.")
        if resp.status_code >= 400:
            msg = _error_message(resp)
            raise AIError(f"Gemini trả lỗi HTTP {resp.status_code}: {msg}")

        data = resp.json()
        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            reason = (data.get("promptFeedback") or {}).get("blockReason") or (
                (data.get("candidates") or [{}])[0].get("finishReason")
            )
            raise _Retryable(f"Gemini không trả về nội dung (lý do: {reason or 'không rõ'}).")

    def generate(self, req: OutlineRequest) -> Presentation:
        contents = [{"role": "user", "parts": [{"text": build_user_prompt(req)}]}]
        last_error = "unknown"
        models = [self.model, *self.fallback_models]
        model = models.pop(0)
        attempts = settings.ai_max_attempts + len(self.fallback_models)
        for attempt in range(1, attempts + 1):
            try:
                raw = self._post(contents, with_image_prompt=req.max_illustrations > 0, model=model)
            except _Retryable as exc:
                last_error = str(exc)
                log.warning("Gemini attempt %d (%s) failed: %s", attempt, model, exc)
                if exc.status == 503 and models:
                    model = models.pop(0)  # overloaded: switch model right away instead of waiting
                    log.warning("Gemini model overloaded; falling back to %s", model)
                    continue
                if attempt < attempts:
                    # Exponential backoff with jitter; longer wait for rate limits.
                    base = 8 if exc.status == 429 else 2
                    time.sleep(base * (2 ** (attempt - 1)) + random.random())
                continue

            try:
                pres = Presentation.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"JSON không hợp lệ: {exc}"
                log.warning("Gemini attempt %d returned invalid JSON: %s", attempt, exc)
                # Ask the model to repair its own output instead of starting over.
                contents = contents[:1] + [
                    {"role": "model", "parts": [{"text": raw[:20000]}]},
                    {"role": "user", "parts": [{"text": build_repair_prompt(str(exc))}]},
                ]
                continue

            if req.title_hint:
                pres.presentation_title = req.title_hint
            self.used_model = model
            return pres

        raise AIError(f"Không tạo được dàn ý sau {attempts} lần thử. {last_error}")


class _Retryable(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return resp.text[:300]
