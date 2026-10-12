"""Start Video2Slides on this computer and open it in the browser.

    python scripts/run_local.py [--port 8000] [--no-browser]

* Binds to 127.0.0.1 only (not reachable from other machines) unless APP_HOST says otherwise.
* If the app is already running on the port, it just opens the browser again.
* Writes data/server.pid so scripts/windows/stop.ps1 can stop a server running in the background.
Stop with Ctrl+C (or close the window).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def app_running(base: str) -> bool:
    """True when a Video2Slides server already answers at ``base``."""
    try:
        with urllib.request.urlopen(f"{base}/api/config", timeout=2) as resp:
            return resp.status == 200 and "styles" in json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def wait_and_open(base: str, open_browser: bool, timeout: float = 120) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if app_running(base):
            print(f"\n  Video2Slides đã sẵn sàng: {base}\n  Đóng cửa sổ này hoặc nhấn Ctrl+C để dừng.\n", flush=True)
            if open_browser:
                webbrowser.open(base)
            return
        time.sleep(0.5)
    print(f"\n  Máy chủ chưa phản hồi sau {timeout:.0f} giây. Xem thông báo lỗi ở trên.\n", flush=True)


def main() -> int:
    from app.config import settings  # also loads .env, so APP_PORT / APP_HOST can be set there

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8000")))
    ap.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}" if args.host in ("127.0.0.1", "0.0.0.0", "localhost") \
        else f"http://{args.host}:{args.port}"
    if app_running(base):
        print(f"Video2Slides đang chạy sẵn tại {base} — mở trình duyệt.")
        if not args.no_browser:
            webbrowser.open(base)
        return 0
    if not port_free(args.host, args.port):
        print(f"LỖI: cổng {args.port} đang bị chương trình khác dùng. "
              f"Đặt APP_PORT=8010 (hoặc cổng khác) trong .env rồi chạy lại.", file=sys.stderr)
        return 2

    import uvicorn

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pid_file = settings.data_dir / "server.pid"
    pid_file.write_text(json.dumps({"pid": os.getpid(), "port": args.port}), encoding="utf-8")
    threading.Thread(target=wait_and_open, args=(base, not args.no_browser), daemon=True).start()
    try:
        uvicorn.Server(uvicorn.Config("app.main:app", host=args.host, port=args.port, log_level="info")).run()
    finally:
        pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
