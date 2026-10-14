"""Video2Slides web app (FastAPI backend + static frontend) hosted on Modal.

Deploy (administrator only, see docs/MODAL_WEB_DEPLOY_VI.md):
    modal secret create video2slides-web-env --from-dotenv .env --force
    modal deploy infrastructure/modal_web_app.py

Architecture
* ONE CPU container (``max_containers=1``): job state lives in that process (queue + worker thread in
  ``app/jobs.py``), so a second container would not see the first one's jobs.
* ``DATA_DIR`` (uploads, jobs, library, Google tokens) is the ``video2slides-web-data`` Volume; Modal commits
  it in the background and on shutdown, so finished presentations survive restarts and redeploys.
* Heavy work runs on the existing GPU workers (media: FFmpeg + Whisper, LLM: Ollama, images: SDXL) that the app
  calls over proxy-auth HTTPS, so this container needs no GPU and no faster-whisper.
* Environment variables come from the ``video2slides-web-env`` Secret (made from .env); the .env file itself is
  never copied into the image. ``APP_PASSWORD`` is required because the URL is public.
* The container scales to zero after ``V2S_WEB_SCALEDOWN_SEC`` without requests. The page polls job status while
  open, so a job keeps the container alive while someone watches it; set ``V2S_WEB_MIN_CONTAINERS=1`` at deploy
  time to keep it always on (CPU billed 24/7).
* Modal web requests are limited to 150 s, so the browser uploads videos in resumable chunks
  (``app/uploads.py``); each chunk is one short request.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

APP_NAME = os.environ.get("V2S_WEB_APP_NAME", "video2slides-web")
SECRET_NAME = os.environ.get("V2S_WEB_SECRET", "video2slides-web-env")
MIN_CONTAINERS = int(os.environ.get("V2S_WEB_MIN_CONTAINERS", "0"))
SCALEDOWN_SEC = int(os.environ.get("V2S_WEB_SCALEDOWN_SEC", "1200"))
DATA_DIR = "/data"
ROOT = Path(__file__).resolve().parent.parent

image = (
    modal.Image.debian_slim(python_version="3.11")
    # ffmpeg: frame/URL helpers; LibreOffice + fonts: slide preview PNGs (Vietnamese glyphs need Noto/DejaVu).
    .apt_install("ffmpeg", "libreoffice-impress", "fonts-dejavu", "fonts-noto-core")
    .pip_install(
        "fastapi>=0.115,<1.0",
        "uvicorn>=0.30",
        "python-multipart>=0.0.9",
        "pydantic>=2.7,<3",
        "httpx>=0.27",
        "python-pptx>=1.0,<2",
        "Pillow>=10.3",
        "yt-dlp[default]>=2025.10",
        "pypdfium2>=4.30",
    )
    .env({"PYTHONUNBUFFERED": "1"})
    .add_local_dir(ROOT / "app", "/root/app", ignore=["**/__pycache__", "**/*.pyc"])
)

app = modal.App(APP_NAME)
data_volume = modal.Volume.from_name("video2slides-web-data", create_if_missing=True)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
    volumes={DATA_DIR: data_volume},
    cpu=2.0,
    memory=2048,
    min_containers=MIN_CONTAINERS,
    max_containers=1,
    scaledown_window=SCALEDOWN_SEC,
    timeout=24 * 3600,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def web():
    # Set before app.config is imported: Settings reads the environment once.
    os.environ["DATA_DIR"] = DATA_DIR
    os.environ["MODEL_CACHE_DIR"] = f"{DATA_DIR}/.cache/models"
    # No local FFmpeg/Whisper pipeline here: media always goes to the GPU worker.
    os.environ["MEDIA_PROCESSING"] = "modal"
    # Modal only counts HTTP requests as activity: keep the container alive while a job runs, even when
    # the browser tab is closed (otherwise it scales down after SCALEDOWN_SEC and the job is lost).
    os.environ.setdefault("KEEPALIVE_WHILE_BUSY", "1")
    if not os.environ.get("APP_PASSWORD"):
        raise RuntimeError("APP_PASSWORD is required: the Modal URL is public. Add it to the video2slides-web-env Secret.")
    if not os.environ.get("MODAL_MEDIA_WORKER_URL"):
        raise RuntimeError("MODAL_MEDIA_WORKER_URL is required (deploy infrastructure/modal_media_worker.py first).")

    from app.main import app as fastapi_app

    return fastapi_app
