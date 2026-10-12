"""Verify the GPU worker's authentication without starting a GPU (health endpoint only).

    python scripts/check_worker_auth.py

Checks that
1. a request without credentials is rejected (HTTP 401),
2. the token in MODAL_PROXY_TOKEN_ID / MODAL_PROXY_TOKEN_SECRET is accepted (HTTP 200),
3. optionally, a revoked token in OLD_MODAL_PROXY_TOKEN_ID / OLD_MODAL_PROXY_TOKEN_SECRET is rejected
   (use this after rotating the token; remove the OLD_* lines from .env afterwards).
Secret values are never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402  (also loads .env)


def status(client: httpx.Client, url: str, token_id: str | None, secret: str | None) -> int:
    headers = {"Modal-Key": token_id, "Modal-Secret": secret} if token_id else {}
    return client.get(f"{url}/v1/health", headers=headers).status_code


def main() -> int:
    s = config.settings
    if not s.modal_image_worker_url:
        print("MODAL_IMAGE_WORKER_URL chưa được cấu hình.")
        return 2
    ok = True
    with httpx.Client(timeout=60, trust_env=False) as client:
        code = status(client, s.modal_image_worker_url, None, None)
        print(f"[{'OK' if code == 401 else 'LỖI'}] Không có token -> HTTP {code} (mong đợi 401)")
        ok &= code == 401

        code = status(client, s.modal_image_worker_url, s.modal_proxy_token_id, s.modal_proxy_token_secret)
        print(f"[{'OK' if code == 200 else 'LỖI'}] Token hiện tại ({s.modal_proxy_token_id[:5]}…) -> HTTP {code} "
              "(mong đợi 200)")
        ok &= code == 200

        old_id, old_secret = os.environ.get("OLD_MODAL_PROXY_TOKEN_ID"), os.environ.get("OLD_MODAL_PROXY_TOKEN_SECRET")
        if old_id and old_secret:
            if old_id == s.modal_proxy_token_id:
                print("[LỖI] OLD_MODAL_PROXY_TOKEN_ID trùng token hiện tại - chưa tạo token mới?")
                ok = False
            else:
                code = status(client, s.modal_image_worker_url, old_id, old_secret)
                print(f"[{'OK' if code == 401 else 'CẢNH BÁO'}] Token cũ ({old_id[:5]}…) -> HTTP {code} "
                      "(mong đợi 401 sau khi đã xóa token cũ trên Modal dashboard)")
                ok &= code == 401
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
