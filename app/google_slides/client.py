"""Authorised Google REST calls with token refresh, rate-limit backoff and user-safe errors."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

import httpx

from ..config import settings
from . import auth
from .errors import GoogleAuthError, GoogleError

log = logging.getLogger(__name__)

DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SLIDES = "https://slides.googleapis.com/v1"
RETRY_STATUS = {429, 500, 502, 503, 504}


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=settings.google_http_timeout_sec)


# Replaced in tests with a client on an httpx.MockTransport.
http_factory: Callable[[], httpx.Client] = _default_http


class GoogleApi:
    """Thin REST client. One instance per export; pass ``http`` in tests (MockTransport)."""

    def __init__(self, http: httpx.Client | None = None, sleep=time.sleep):
        self._own = http is None
        self.http = http or http_factory()
        self.sleep = sleep
        self.calls = 0

    def close(self) -> None:
        if self._own:
            self.http.close()

    def __enter__(self) -> "GoogleApi":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        refreshed = False
        attempts = settings.google_max_attempts
        for attempt in range(1, attempts + 1):
            headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {auth.access_token(refreshed)}"}
            try:
                self.calls += 1
                resp = self.http.request(method, url, headers=headers, **kw)
            except httpx.TimeoutException:
                if attempt == attempts:
                    raise GoogleError("Google phản hồi quá chậm (timeout). Hãy thử lại.", "timeout") from None
                self._backoff(attempt, None)
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            except httpx.HTTPError as exc:
                raise GoogleError(f"Lỗi kết nối tới Google ({exc.__class__.__name__}).", "network") from None
            kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
            if resp.status_code == 401 and not refreshed:
                refreshed = True  # access token expired early or was revoked: refresh once
                continue
            if resp.status_code == 401:
                raise GoogleAuthError("Google từ chối phiên đăng nhập. Hãy kết nối lại tài khoản Google.", "revoked")
            if resp.status_code in RETRY_STATUS or (resp.status_code == 403 and _reason(resp) in (
                    "rateLimitExceeded", "userRateLimitExceeded")):
                if attempt == attempts:
                    raise GoogleError("Google đang giới hạn tần suất hoặc tạm thời lỗi. Hãy thử lại sau ít phút.",
                                      "rate_limited", resp.status_code)
                self._backoff(attempt, resp.headers.get("retry-after"))
                continue
            if resp.status_code == 403:
                raise GoogleError(_forbidden_message(resp), "forbidden", 403)
            if resp.status_code == 404:
                raise GoogleError("Không tìm thấy tệp trên Google Drive (có thể đã bị xóa).", "not_found", 404)
            if resp.status_code >= 400:
                raise GoogleError(f"Google trả lỗi HTTP {resp.status_code}: {_message(resp)}", "api_error",
                                  resp.status_code)
            return resp
        raise GoogleError("Không gọi được Google API.", "api_error")

    def json(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        return self.request(method, url, **kw).json()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        wait = max(wait, 2 ** attempt + random.random())
        log.info("Google API busy, retrying in %.1fs", wait)
        self.sleep(min(wait, 60))


def _error(resp: httpx.Response) -> dict[str, Any]:
    try:
        return (resp.json() or {}).get("error") or {}
    except ValueError:
        return {}


def _message(resp: httpx.Response) -> str:
    err = _error(resp)
    return str(err.get("message") or resp.text[:200] if isinstance(err, dict) else err)[:300]


def _reason(resp: httpx.Response) -> str:
    err = _error(resp)
    errors = err.get("errors") if isinstance(err, dict) else None
    if errors and isinstance(errors, list):
        return str(errors[0].get("reason") or "")
    for d in (err.get("details") or []) if isinstance(err, dict) else []:
        if d.get("reason"):
            return str(d["reason"])
    return ""


def _forbidden_message(resp: httpx.Response) -> str:
    text = _message(resp)
    if "has not been used" in text or "is disabled" in text or _reason(resp) in ("accessNotConfigured",
                                                                                 "SERVICE_DISABLED"):
        return ("Google Drive API hoặc Google Slides API chưa được bật trong dự án Google Cloud. "
                "Hãy bật cả hai API (xem docs/GOOGLE_SLIDES_SETUP_VI.md) rồi thử lại.")
    if _reason(resp) in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
        return "Ứng dụng chưa có quyền tạo tệp trên Google Drive. Hãy kết nối lại tài khoản Google."
    return f"Google từ chối yêu cầu (HTTP 403): {text}"
