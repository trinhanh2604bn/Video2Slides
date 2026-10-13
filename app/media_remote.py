"""Cloud video processing: FFmpeg + GPU faster-whisper on the private Modal media worker.

The backend drives the worker (infrastructure/modal_media_worker.py) step by step and stores a checkpoint after
each step in ``remote_media.json`` inside the job folder:

    uploaded -> preprocessed (audio on Modal, frames + metadata here) -> transcribed (transcript.json here)

Server-to-server only, with the Modal proxy-auth token in headers (never in URLs, logs or the browser).
The local MP4 stays in the job folder until the job completes, so every remote step can be redone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import config

log = logging.getLogger(__name__)

TRANSCRIBE_IMPL_VERSION = "v2s-transcribe-2"  # 2: batched decoding + sentence segments from word timestamps


class RemoteMediaError(Exception):
    """User-safe failure of the Modal media worker. ``code`` is stable (e.g. ``cuda_unavailable``)."""

    def __init__(self, message: str, code: str = "media_worker_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def transcript_key(source_sha256: str, **transcription_settings) -> str:
    """Identity of a transcript: same source + same transcription settings (location, model, precision,
    language, decoding options, preprocessing) + same implementation version."""
    blob = json.dumps({"src": source_sha256, **transcription_settings, "impl": TRANSCRIBE_IMPL_VERSION},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


@dataclass
class Waiter:
    check_cancel: Callable[[], None] | None = None
    on_tick: Callable[[float], None] | None = None
    # Called once when the GPU container has started the call (until then Modal is allocating a GPU).
    on_started: Callable[[], None] | None = None


class ModalMediaClient:
    def __init__(self, base_url: str | None = None, token_id: str | None = None, token_secret: str | None = None,
                 client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        s = config.settings
        self.base_url = (base_url or s.modal_media_worker_url).rstrip("/")
        self._id = token_id or s.modal_proxy_token_id
        self._secret = token_secret or s.modal_proxy_token_secret
        self._client = client
        self._sleep = sleep
        if not self.base_url or not self._id or not self._secret:
            raise RemoteMediaError("Chưa cấu hình xử lý video trên Modal (MODAL_MEDIA_WORKER_URL, "
                                   "MODAL_PROXY_TOKEN_ID, MODAL_PROXY_TOKEN_SECRET).", "not_configured")
        if not self.base_url.startswith("https://"):
            raise RemoteMediaError("MODAL_MEDIA_WORKER_URL phải dùng https://.", "not_configured")

    # ------------------------------------------------------------------ transport

    def _request(self, method: str, path: str, timeout: float | None = None, **kw) -> httpx.Response:
        client = self._client or httpx.Client(timeout=timeout or config.settings.modal_http_timeout_sec,
                                              trust_env=False)
        headers = {"Modal-Key": self._id, "Modal-Secret": self._secret, **kw.pop("headers", {})}
        try:
            resp = client.request(method, f"{self.base_url}{path}", headers=headers, **kw)
        except httpx.TimeoutException as exc:
            raise RemoteMediaError("Media worker trên Modal phản hồi quá chậm.", "transport", True) from exc
        except httpx.HTTPError as exc:
            raise RemoteMediaError(f"Không kết nối được media worker trên Modal ({exc.__class__.__name__}).",
                                   "transport", True) from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code in (401, 403):
            raise RemoteMediaError("Token truy cập Modal không hợp lệ (kiểm tra MODAL_PROXY_TOKEN_*).", "auth")
        if resp.status_code in (400, 413, 422):
            raise RemoteMediaError(f"Media worker từ chối yêu cầu: {_detail(resp)}", "invalid_request")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RemoteMediaError(f"Media worker trên Modal tạm thời lỗi (HTTP {resp.status_code}).", "transport", True)
        return resp

    def health(self) -> dict:
        return self._request("GET", "/v1/health").json() or {}

    # ------------------------------------------------------------------ steps

    def upload(self, path: Path, sha256: str, w: Waiter) -> dict:
        upload_id = self._request("POST", "/v1/uploads").json()["upload_id"]
        chunk = config.settings.modal_media_upload_chunk_mb * 1024 * 1024
        size, index = path.stat().st_size, 0
        started = time.monotonic()
        with path.open("rb") as fh:
            while data := fh.read(chunk):
                if w.check_cancel:
                    w.check_cancel()
                for attempt in range(3):  # bounded retry per chunk (idempotent PUT)
                    try:
                        self._request("PUT", f"/v1/uploads/{upload_id}/chunks/{index}", content=data, timeout=300,
                                      headers={"Content-Type": "application/octet-stream"})
                        break
                    except RemoteMediaError as exc:
                        if not exc.retryable or attempt == 2:
                            raise
                        self._sleep(2 * (attempt + 1))
                index += 1
                if w.on_tick:
                    w.on_tick(min(1.0, index * chunk / max(size, 1)))
        return {"upload_id": upload_id, "chunks": index, "size": size, "sha256": sha256,
                "upload_sec": round(time.monotonic() - started, 2)}

    def preprocess(self, up: dict, params: dict, w: Waiter) -> dict:
        call = self._request("POST", f"/v1/uploads/{up['upload_id']}/complete",
                             json={"sha256": up["sha256"], "size": up["size"], "chunks": up["chunks"],
                                   "params": params}).json()["call_id"]
        return self._wait(call, w, progress_of=None)

    def transcribe(self, upload_id: str, params: dict, w: Waiter) -> dict:
        call = self._request("POST", "/v1/transcribe", json={"upload_id": upload_id, "params": params}).json()["call_id"]
        return self._wait(call, w, progress_of=upload_id)

    def delete(self, upload_id: str) -> None:
        try:
            self._request("DELETE", f"/v1/uploads/{upload_id}")
        except RemoteMediaError as exc:
            log.warning("could not delete Modal upload: %s", exc.code)

    def _wait(self, call_id: str, w: Waiter, progress_of: str | None) -> dict:
        deadline = time.monotonic() + config.settings.modal_media_timeout_sec
        started = False
        while True:
            try:
                if w.check_cancel:
                    w.check_cancel()
            except BaseException:
                try:
                    self._request("POST", f"/v1/cancel/{call_id}")
                except RemoteMediaError:
                    pass
                raise
            if time.monotonic() > deadline:
                self._request("POST", f"/v1/cancel/{call_id}")
                raise RemoteMediaError(f"Xử lý video trên Modal quá {config.settings.modal_media_timeout_sec} giây "
                                       "(MODAL_MEDIA_TIMEOUT_SECONDS).", "timeout", True)
            try:
                data = self._request("GET", f"/v1/result/{call_id}").json() or {}
            except RemoteMediaError as exc:
                if not exc.retryable:
                    raise
                self._sleep(config.settings.modal_poll_interval_sec)
                continue
            state = data.get("status")
            if state == "done":
                return data.get("result") or {}
            if state == "failed":
                raise RemoteMediaError(str(data.get("error") or "Media worker báo lỗi.")[:300],
                                       str(data.get("error_code") or "worker_error"), bool(data.get("retryable")))
            if state == "expired":
                raise RemoteMediaError("Kết quả trên media worker đã hết hạn.", "expired", True)
            if progress_of and (w.on_tick or w.on_started):
                try:
                    p = self._request("GET", f"/v1/progress/{progress_of}").json() or {}
                    if p and not started:
                        started = True
                        if w.on_started:
                            w.on_started()
                    if p.get("total_sec") and w.on_tick:
                        w.on_tick(float(p["done_sec"]) / float(p["total_sec"]))
                except (RemoteMediaError, ValueError, KeyError):
                    pass
            self._sleep(config.settings.modal_poll_interval_sec)


def save_frames(result: dict, frames_dir: Path) -> list:
    """Write the worker's candidate frames and keep the distinct ones (same rules as local extraction)."""
    from .video import select_frames

    frames_dir.mkdir(parents=True, exist_ok=True)
    grabbed = []
    for i, f in enumerate(result.get("frames") or []):
        target = frames_dir / f"cand_{i:02d}.jpg"
        target.write_bytes(base64.b64decode(f["jpeg_b64"]))
        grabbed.append((target, float(f["timestamp"])))
    return select_frames(grabbed, frames_dir)


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", ""))[:200]
    except ValueError:
        return ""
