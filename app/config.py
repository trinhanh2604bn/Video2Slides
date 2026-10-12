"""Runtime configuration, read once from environment variables.

Every limit is configurable so the MVP defaults can be tightened for small
Replit machines or relaxed on bigger hosts without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader for local development (Replit uses Secrets instead).

    Existing environment variables always win, so Secrets are never overridden.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Storage
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", BASE_DIR / "data")))
    model_cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("MODEL_CACHE_DIR", BASE_DIR / ".cache" / "models"))
    )
    job_ttl_hours: float = field(default_factory=lambda: _float("JOB_TTL_HOURS", 24))

    # Upload / media limits
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 80))
    max_duration_sec: int = field(default_factory=lambda: _int("MAX_DURATION_SEC", 600))
    max_frames: int = field(default_factory=lambda: _int("MAX_FRAMES", 8))
    frame_width: int = field(default_factory=lambda: _int("FRAME_WIDTH", 1280))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _int("FFMPEG_TIMEOUT_SEC", 180))
    max_queue_size: int = field(default_factory=lambda: _int("MAX_QUEUE_SIZE", 3))
    audio_normalize: bool = field(default_factory=lambda: _bool("AUDIO_NORMALIZE", True))

    # Speech recognition
    whisper_model: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL", "base"))
    whisper_compute_type: str = field(default_factory=lambda: os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))
    whisper_cpu_threads: int = field(default_factory=lambda: _int("WHISPER_CPU_THREADS", 2))
    whisper_beam_size: int = field(default_factory=lambda: _int("WHISPER_BEAM_SIZE", 1))
    default_language: str = field(default_factory=lambda: os.environ.get("DEFAULT_LANGUAGE", "vi"))

    # AI provider
    ai_provider: str = field(default_factory=lambda: os.environ.get("AI_PROVIDER", "auto"))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
    ai_timeout_sec: int = field(default_factory=lambda: _int("AI_TIMEOUT_SEC", 120))
    ai_max_attempts: int = field(default_factory=lambda: _int("AI_MAX_ATTEMPTS", 3))

    # Access control: when set, every API call must send this shared password.
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))

    # Video URL import (YouTube / Facebook)
    url_import_enabled: bool = field(default_factory=lambda: _bool("URL_IMPORT_ENABLED", True))
    # off | ytdlp. Off by default: YouTube's Terms do not authorise third-party downloading.
    youtube_retrieval: str = field(default_factory=lambda: os.environ.get("YOUTUBE_RETRIEVAL", "off").lower())
    # off | graph_api | ytdlp. graph_api only works for videos the token may read (e.g. your own Page).
    facebook_retrieval: str = field(default_factory=lambda: os.environ.get("FACEBOOK_RETRIEVAL", "graph_api").lower())
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    facebook_access_token: str = field(default_factory=lambda: os.environ.get("FACEBOOK_ACCESS_TOKEN", ""))
    facebook_graph_version: str = field(default_factory=lambda: os.environ.get("FACEBOOK_GRAPH_VERSION", "v23.0"))
    url_max_duration_sec: int = field(default_factory=lambda: _int("URL_MAX_DURATION_SEC", 600))
    url_max_download_mb: int = field(default_factory=lambda: _int("URL_MAX_DOWNLOAD_MB", 80))
    url_max_height: int = field(default_factory=lambda: _int("URL_MAX_HEIGHT", 480))
    download_timeout_sec: int = field(default_factory=lambda: _int("DOWNLOAD_TIMEOUT_SEC", 300))
    download_socket_timeout_sec: int = field(default_factory=lambda: _int("DOWNLOAD_SOCKET_TIMEOUT_SEC", 20))
    download_retries: int = field(default_factory=lambda: _int("DOWNLOAD_RETRIES", 2))
    max_concurrent_downloads: int = field(default_factory=lambda: max(1, _int("MAX_CONCURRENT_DOWNLOADS", 1)))
    # 0 = no limit. Requests per minute per client IP for the URL endpoints.
    url_rate_limit_per_min: int = field(default_factory=lambda: _int("URL_RATE_LIMIT_PER_MIN", 0))

    # AI illustrations (self-hosted SDXL on Modal). Off until an admin configures the GPU worker.
    ai_images_enabled: bool = field(default_factory=lambda: _bool("AI_IMAGES_ENABLED", False))
    # modal_sdxl | mock (mock = local placeholder images, for development and tests only)
    image_provider: str = field(default_factory=lambda: os.environ.get("IMAGE_PROVIDER", "modal_sdxl").lower())
    modal_image_worker_url: str = field(default_factory=lambda: os.environ.get("MODAL_IMAGE_WORKER_URL", "").rstrip("/"))
    # Modal *proxy auth* token: can only call the worker's web endpoint, not deploy or run code.
    modal_proxy_token_id: str = field(default_factory=lambda: os.environ.get("MODAL_PROXY_TOKEN_ID", ""))
    modal_proxy_token_secret: str = field(default_factory=lambda: os.environ.get("MODAL_PROXY_TOKEN_SECRET", ""))
    image_kill_switch: bool = field(default_factory=lambda: _bool("IMAGE_KILL_SWITCH", False))
    image_max_per_presentation: int = field(default_factory=lambda: max(0, min(8, _int("IMAGE_MAX_PER_PRESENTATION", 8))))
    image_daily_limit: int = field(default_factory=lambda: _int("IMAGE_DAILY_LIMIT", 40))
    image_monthly_limit: int = field(default_factory=lambda: _int("IMAGE_MONTHLY_LIMIT", 300))
    image_monthly_budget_usd: float = field(default_factory=lambda: _float("IMAGE_MONTHLY_BUDGET_USD", 5.0))
    # Cost model used for pre-batch estimates (Modal L4 list price; verify on modal.com/pricing).
    image_gpu_price_per_sec: float = field(default_factory=lambda: _float("IMAGE_GPU_PRICE_PER_SEC", 0.000222))
    image_est_seconds_per_image: float = field(default_factory=lambda: _float("IMAGE_EST_SECONDS_PER_IMAGE", 15))
    image_est_batch_overhead_sec: float = field(default_factory=lambda: _float("IMAGE_EST_BATCH_OVERHEAD_SEC", 210))
    image_width: int = field(default_factory=lambda: _int("IMAGE_WIDTH", 1344))
    image_height: int = field(default_factory=lambda: _int("IMAGE_HEIGHT", 768))
    image_steps: int = field(default_factory=lambda: _int("IMAGE_STEPS", 25))
    image_guidance: float = field(default_factory=lambda: _float("IMAGE_GUIDANCE", 6.0))
    image_batch_timeout_sec: int = field(default_factory=lambda: _int("IMAGE_BATCH_TIMEOUT_SEC", 900))
    image_http_timeout_sec: int = field(default_factory=lambda: _int("IMAGE_HTTP_TIMEOUT_SEC", 30))
    image_poll_interval_sec: float = field(default_factory=lambda: _float("IMAGE_POLL_INTERVAL_SEC", 3))
    image_max_attempts: int = field(default_factory=lambda: max(1, min(3, _int("IMAGE_MAX_ATTEMPTS", 2))))
    # fail = the job fails when an illustration cannot be produced; skip = keep the slide without it (with a notice)
    image_on_failure: str = field(default_factory=lambda: os.environ.get("IMAGE_ON_FAILURE", "fail").lower())
    image_cache_ttl_hours: float = field(default_factory=lambda: _float("IMAGE_CACHE_TTL_HOURS", 168))
    image_max_regenerations_per_job: int = field(default_factory=lambda: _int("IMAGE_MAX_REGENERATIONS_PER_JOB", 10))
    # Enables /api/admin/* (usage report, kill switch). Empty = admin API disabled.
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN", ""))

    @property
    def illustrations_dir(self) -> Path:
        return self.data_dir / "illustrations"

    @property
    def url_max_download_bytes(self) -> int:
        return self.url_max_download_mb * 1024 * 1024

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


settings = Settings()

ALLOWED_WHISPER_MODELS = {"tiny", "base", "small"}
SUPPORTED_LANGUAGES = {"vi": "Tiếng Việt", "en": "English", "auto": "Tự động nhận diện"}
