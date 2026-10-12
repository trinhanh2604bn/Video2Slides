"""ModalSDXLImageProvider: talks to the private Modal worker (infrastructure/modal_image_worker.py).

Server-to-server only. Requests carry a Modal *proxy auth token* (``Modal-Key`` /
``Modal-Secret`` headers): it can call this one web endpoint but cannot deploy
apps or run code in the workspace, which keeps the Replit credential least-privilege.
The browser never sees it.
"""

from __future__ import annotations

import base64
import binascii
import logging

import httpx

from .. import config
from .base import ImageGenerationError, ImageGenerationProvider, ImageJobStatus, ImageRequest, ImageResult

log = logging.getLogger(__name__)

MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"


class ModalSDXLImageProvider(ImageGenerationProvider):
    name = "modal_sdxl"
    model_id = MODEL_ID

    def __init__(self, base_url: str | None = None, token_id: str | None = None, token_secret: str | None = None,
                 client: httpx.Client | None = None):
        s = config.settings
        self.base_url = (base_url or s.modal_image_worker_url).rstrip("/")
        self._token_id = token_id or s.modal_proxy_token_id
        self._token_secret = token_secret or s.modal_proxy_token_secret
        if not self.base_url or not self._token_id or not self._token_secret:
            raise ImageGenerationError(
                "Chưa cấu hình GPU worker (MODAL_IMAGE_WORKER_URL, MODAL_PROXY_TOKEN_ID, MODAL_PROXY_TOKEN_SECRET).",
                "not_configured")
        if not self.base_url.startswith("https://"):
            raise ImageGenerationError("MODAL_IMAGE_WORKER_URL phải dùng https://.", "not_configured")
        self._client = client

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        client = self._client or httpx.Client(timeout=config.settings.image_http_timeout_sec, trust_env=False)
        headers = {"Modal-Key": self._token_id, "Modal-Secret": self._token_secret}
        try:
            resp = client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ImageGenerationError("GPU worker phản hồi quá chậm.", "transport", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ImageGenerationError(f"Không kết nối được GPU worker ({exc.__class__.__name__}).", "transport",
                                       retryable=True) from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code in (401, 403):
            raise ImageGenerationError("Token truy cập GPU worker không hợp lệ (kiểm tra MODAL_PROXY_TOKEN_*).",
                                       "auth")
        if resp.status_code in (400, 422):
            raise ImageGenerationError(f"GPU worker từ chối yêu cầu: {_detail(resp)}", "invalid_params")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ImageGenerationError(f"GPU worker tạm thời lỗi (HTTP {resp.status_code}).", "transport",
                                       retryable=True)
        return resp

    # ------------------------------------------------------------------ interface
    def submit(self, req: ImageRequest, idempotency_key: str | None = None) -> str:
        body = {"request_id": idempotency_key or req.output_id, "params": req.model_params()}
        resp = self._request("POST", "/v1/generate", json=body)
        if resp.status_code != 200:
            raise ImageGenerationError(f"GPU worker trả lỗi HTTP {resp.status_code}.", "transport", retryable=True)
        call_id = (resp.json() or {}).get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ImageGenerationError("GPU worker không trả về mã công việc.", "transport", retryable=True)
        return call_id

    def get_job_status(self, job_ref: str) -> ImageJobStatus:
        resp = self._request("GET", f"/v1/result/{job_ref}")
        if resp.status_code == 404:
            return ImageJobStatus("expired", error="Kết quả trên GPU worker đã hết hạn.", error_code="expired",
                                  retryable=True)
        if resp.status_code != 200:
            raise ImageGenerationError(f"GPU worker trả lỗi HTTP {resp.status_code}.", "transport", retryable=True)
        body = resp.json() or {}
        state = body.get("status")
        if state == "pending":
            return ImageJobStatus("pending")
        if state == "expired":
            return ImageJobStatus("expired", error="Kết quả trên GPU worker đã hết hạn.", error_code="expired",
                                  retryable=True)
        if state == "failed":
            return ImageJobStatus("failed", error=str(body.get("error") or "GPU worker báo lỗi.")[:300],
                                  error_code=str(body.get("error_code") or "worker_error"),
                                  retryable=bool(body.get("retryable")))
        if state == "done":
            try:
                data = base64.b64decode(body.get("image_b64") or "", validate=True)
            except (binascii.Error, ValueError):
                return ImageJobStatus("failed", error="Dữ liệu ảnh hỏng.", error_code="invalid_image", retryable=True)
            return ImageJobStatus("done", result=ImageResult(
                data=data,
                format=str(body.get("format") or "jpeg"),
                width=int(body.get("width") or 0),
                height=int(body.get("height") or 0),
                model=str(body.get("model") or self.model_id),
                seed=int(body.get("seed") or 0),
                metrics={k: v for k, v in (body.get("metrics") or {}).items() if isinstance(v, (int, float, str))},
            ))
        raise ImageGenerationError("Phản hồi GPU worker không hợp lệ.", "transport", retryable=True)

    def cancel(self, job_ref: str) -> None:
        try:
            self._request("POST", f"/v1/cancel/{job_ref}")
        except ImageGenerationError as exc:
            log.warning("cancel of %s failed: %s", job_ref, exc.code)

    def health(self) -> dict:
        resp = self._request("GET", "/v1/health")
        return {"ok": resp.status_code == 200, "provider": self.name, **(resp.json() if resp.status_code == 200 else {})}


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))[:200]
    except ValueError:
        return ""
