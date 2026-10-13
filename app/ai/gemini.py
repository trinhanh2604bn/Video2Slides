"""Google Gemini provider using the public REST API (no extra SDK dependency).

Every failure is raised as a ``GeminiError`` with a ``kind`` the router (``app.ai.router``) uses to decide
whether falling back to Ollama on Modal is appropriate:

* ``auth``          invalid / unauthorised key (also HTTP 400 API_KEY_INVALID)  -> fallback, no retry
* ``config``        unknown model, unsupported endpoint/location                 -> fallback + config warning
* ``quota``         quota exhausted (daily / zero limit)                         -> fallback, no retry
* ``rate_limit``    short per-minute limit                                       -> bounded retry, then fallback
* ``server`` / ``network`` / ``timeout``  transient                              -> bounded retry, then fallback
* ``invalid_output`` JSON that fails validation after the bounded repairs        -> fallback for that unit
* ``safety``        explicit safety refusal                                      -> NEVER bypassed with another provider
* ``bad_request``   our own request was rejected (application bug)               -> reported, no fallback

Budgets: at most ``GEMINI_MAX_ATTEMPTS`` (<= 2) request attempts and ``LLM_MAX_REPAIR_ATTEMPTS`` (<= 2) repair
attempts, all inside one wall-clock budget (``GEMINI_TOTAL_BUDGET_SECONDS``), so retries cannot multiply.
The API key travels in a header only and is never logged; raw API responses are never shown to users.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time

import httpx
from pydantic import ValidationError

from ..config import settings
from .base import AIError, OutlineProvider, OutlineRequest
from .prompt import SYSTEM_PROMPT, build_repair_prompt, build_user_prompt
from .schema import Presentation, gemini_response_schema

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Part of the output provenance / cache identity of Gemini outlines; bump when prompt.py or the schema changes.
PROMPT_VERSION = "gemini-v2"  # v2: grounding rule

RETRYABLE_KINDS = ("rate_limit", "server", "network", "timeout")
SAFETY_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}


class GeminiError(AIError):
    def __init__(self, message: str, kind: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message, f"gemini_{kind}")
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_KINDS


class GeminiProvider(OutlineProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None, client: httpx.Client | None = None,
                 fallback_models: tuple[str, ...] | None = None):
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        # Used when the current model is overloaded (HTTP 503), inside the same attempt budget.
        fallbacks = settings.gemini_fallback_models if fallback_models is None else fallback_models
        self.fallback_models = tuple(m for m in fallbacks if m and m != self.model)
        self.used_model = self.model
        if not self.api_key:
            raise GeminiError("Thiếu GEMINI_API_KEY. Hãy thêm khóa vào tệp .env (hoặc Secrets của máy chủ đám mây).",
                              "missing_key")
        self._client = client
        self.attempts: list[dict] = []  # operational record, no content and no key
        self.repairs = 0

    # ------------------------------------------------------------------ transport

    def _post(self, contents: list[dict], with_image_prompt: bool, model: str, timeout: float) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                "responseSchema": gemini_response_schema(include_image_prompt=with_image_prompt),
            },
        }
        client = self._client or httpx.Client(timeout=timeout)
        try:
            # Key goes in a header, never in the URL, so it can't leak into logs.
            resp = client.post(API_URL.format(model=model),
                               headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                               json=body, timeout=timeout)
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
            data = resp.json()
        except ValueError as exc:
            raise GeminiError("Gemini trả về dữ liệu không đọc được.", "server", resp.status_code) from exc
        return extract_text(data)

    # ------------------------------------------------------------------ bounded generation

    def generate(self, req: OutlineRequest) -> Presentation:
        s = settings
        started = time.monotonic()
        deadline = started + max(10, s.gemini_total_budget_sec)
        first = [{"role": "user", "parts": [{"text": build_user_prompt(req)}]}]
        contents = first
        models = [self.model, *self.fallback_models]
        model = models.pop(0)
        transport_failures = 0
        self.repairs = 0
        self.attempts = []
        max_requests = s.gemini_max_attempts + s.llm_max_repair_attempts
        last: GeminiError | None = None
        while len(self.attempts) < max_requests:
            if req.check_cancel:
                req.check_cancel()
            remaining = deadline - time.monotonic()
            if remaining < 5:
                break
            t0 = time.monotonic()
            try:
                raw = self._post(contents, req.max_illustrations > 0, model, min(s.ai_timeout_sec, remaining))
            except GeminiError as exc:
                self._record(model, exc.kind, exc.status, t0)
                last = exc
                if not exc.retryable:
                    raise
                transport_failures += 1
                if transport_failures >= s.gemini_max_attempts:
                    raise
                if exc.status == 503 and models:
                    model = models.pop(0)  # overloaded: switch model right away instead of waiting
                    log.warning("Gemini model overloaded; trying %s", model)
                    continue
                wait = exc.retry_after if exc.retry_after is not None else 2 * transport_failures + random.random()
                if wait > s.gemini_max_retry_after_sec or wait > deadline - time.monotonic() - 10:
                    raise  # a retry this late is unlikely to help: fall back promptly
                _sleep(wait, req.check_cancel)
                continue

            try:
                pres = Presentation.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                self._record(model, "invalid_output", 200, t0)
                last = GeminiError(f"Gemini trả về JSON chưa hợp lệ: {_short(exc)}", "invalid_output")
                self.repairs += 1
                if self.repairs > s.llm_max_repair_attempts:
                    raise last from None
                log.warning("Gemini answer %d failed validation; repair %d", len(self.attempts), self.repairs)
                # Ask the model to repair its own output instead of starting over.
                contents = first + [
                    {"role": "model", "parts": [{"text": raw[:20000]}]},
                    {"role": "user", "parts": [{"text": build_repair_prompt(str(exc))}]},
                ]
                continue

            self._record(model, "ok", 200, t0)
            if req.title_hint:
                pres.presentation_title = req.title_hint
            self.used_model = model
            return pres

        if last is not None:
            raise last
        raise GeminiError("Gemini hết thời gian cho phép của bước này.", "timeout")

    def _record(self, model: str, outcome: str, status: int | None, t0: float) -> None:
        self.attempts.append({"model": model, "outcome": outcome, "status": status,
                              "seconds": round(time.monotonic() - t0, 2)})

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.used_model, "prompt_version": PROMPT_VERSION,
                "requests": len(self.attempts), "repairs": self.repairs, "attempts": self.attempts}


# ---------------------------------------------------------------------- response classification

def extract_text(data: dict) -> str:
    """Answer text, or a GeminiError: safety refusal (never retried elsewhere) or an empty/odd answer."""
    feedback = (data.get("promptFeedback") or {}).get("blockReason")
    if feedback:
        if feedback in SAFETY_REASONS or feedback == "OTHER":
            raise GeminiError("Gemini từ chối xử lý nội dung này vì lý do an toàn. Ứng dụng không tự chuyển sang "
                              "mô hình khác để vượt qua từ chối; hãy kiểm tra nội dung video.", "safety")
        raise GeminiError(f"Gemini chặn yêu cầu (lý do: {feedback}).", "safety")
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("Gemini không trả về nội dung.", "server")
    cand = candidates[0]
    reason = cand.get("finishReason") or ""
    if reason in SAFETY_REASONS:
        raise GeminiError("Gemini dừng câu trả lời vì lý do an toàn. Ứng dụng không tự chuyển sang mô hình khác "
                          "để vượt qua từ chối; hãy kiểm tra nội dung video.", "safety")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        raise GeminiError(f"Gemini trả về câu trả lời rỗng (lý do: {reason or 'không rõ'}).", "server")
    # MAX_TOKENS / RECITATION leave a cut or partial answer: validation fails and the repair path handles it.
    return text


def classify_http_error(resp: httpx.Response, model: str) -> GeminiError:
    code = resp.status_code
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    status = str(err.get("status") or "")
    message = str(err.get("message") or "")
    details = err.get("details") or []
    reasons = {str(d.get("reason") or "") for d in details if isinstance(d, dict)}
    retry_after = _retry_after(resp, details)

    if code in (401, 403) or "API_KEY_INVALID" in reasons or "API key not valid" in message:
        return GeminiError("GEMINI_API_KEY không hợp lệ hoặc không có quyền truy cập model.", "auth", code)
    if code == 404:
        return GeminiError(f"Model Gemini '{model}' không tồn tại hoặc không còn dùng được. Kiểm tra GEMINI_MODEL.",
                           "config", code)
    if code == 429:
        if _quota_exhausted(details, message):
            return GeminiError("Gemini đã hết hạn mức (quota) sử dụng.", "quota", code, retry_after)
        return GeminiError("Gemini đang giới hạn tốc độ (HTTP 429).", "rate_limit", code, retry_after)
    if code >= 500:
        return GeminiError(f"Gemini tạm thời không khả dụng (HTTP {code}).", "server", code, retry_after)
    if code == 400 and (status == "FAILED_PRECONDITION" or "location is not supported" in message.lower()):
        return GeminiError("Gemini không khả dụng cho tài khoản/khu vực này.", "config", code)
    return GeminiError(f"Gemini từ chối yêu cầu (HTTP {code} {status}).".replace(" )", ")"), "bad_request", code)


def _quota_exhausted(details: list, message: str) -> bool:
    """Daily / zero-limit quota (waiting seconds will not help) vs. a short per-minute rate limit."""
    for d in details:
        if not isinstance(d, dict):
            continue
        for v in d.get("violations") or []:
            quota_id = str(v.get("quotaId") or "") + str(v.get("quotaMetric") or "")
            if "PerDay" in quota_id or str(v.get("quotaValue") or "") == "0":
                return True
    low = message.lower()
    return "limit: 0" in low or "per day" in low or "billing" in low


def _retry_after(resp: httpx.Response, details: list) -> float | None:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    for d in details:
        if isinstance(d, dict) and d.get("retryDelay"):
            m = re.match(r"^([\d.]+)s$", str(d["retryDelay"]))
            if m:
                return float(m.group(1))
    return None


def _sleep(seconds: float, check_cancel) -> None:
    left = max(0.0, seconds)
    while left > 0:
        if check_cancel:
            check_cancel()
        step = min(0.5, left)
        time.sleep(step)
        left -= step


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
    return str(exc)[:200]
