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
    # true = finished presentations stay in the library (Reading Mode, re-download)
    keep_finished_presentations: bool = field(default_factory=lambda: _bool("KEEP_FINISHED_PRESENTATIONS", True))
    # Hours an uploaded/downloaded source video is kept after the deck is done, so Reading Mode timestamps can
    # play it in the app (the file is renamed, not copied; the periodic cleanup deletes it). 0 = delete at once.
    keep_source_video_hours: float = field(default_factory=lambda: max(0.0, _float("KEEP_SOURCE_VIDEO_HOURS", 24)))

    # Upload / media limits
    # The video file only stays on this computer; with cloud processing only its audio track is uploaded.
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 6144))
    max_duration_sec: int = field(default_factory=lambda: _int("MAX_DURATION_SEC", 7200))  # 120 minutes
    # Free disk space kept after accepting an upload (the video, frames and outputs live in DATA_DIR).
    min_free_disk_mb: int = field(default_factory=lambda: _int("MIN_FREE_DISK_MB", 2048))
    max_frames: int = field(default_factory=lambda: _int("MAX_FRAMES", 8))
    frame_width: int = field(default_factory=lambda: _int("FRAME_WIDTH", 1280))
    ffmpeg_timeout_sec: int = field(default_factory=lambda: _int("FFMPEG_TIMEOUT_SEC", 900))
    max_queue_size: int = field(default_factory=lambda: _int("MAX_QUEUE_SIZE", 3))
    audio_normalize: bool = field(default_factory=lambda: _bool("AUDIO_NORMALIZE", True))

    # Speech recognition
    whisper_model: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL", "base"))
    whisper_compute_type: str = field(default_factory=lambda: os.environ.get("WHISPER_COMPUTE_TYPE", "int8"))
    whisper_cpu_threads: int = field(default_factory=lambda: _int("WHISPER_CPU_THREADS", 2))
    whisper_beam_size: int = field(default_factory=lambda: _int("WHISPER_BEAM_SIZE", 1))
    default_language: str = field(default_factory=lambda: os.environ.get("DEFAULT_LANGUAGE", "vi"))

    # LLM provider routing (LLM_PROVIDER wins over the older AI_PROVIDER name):
    #   modal_ollama = Ollama on Modal (GPU) only (default: no time lost trying Gemini first, GPU pre-warmed
    #                  while Whisper runs)
    #   auto         = Gemini first when GEMINI_API_KEY is set, Ollama on Modal otherwise or after an eligible
    #                  Gemini failure | gemini = Gemini only (explicit errors)
    #   ollama       = Ollama on this computer (explicit only, never chosen automatically) | offline = no AI
    ai_provider: str = field(default_factory=lambda: (
        os.environ.get("LLM_PROVIDER") or os.environ.get("AI_PROVIDER") or "modal_ollama").strip().lower())
    # Gemini budget inside the router: request attempts (transport) and repair attempts (invalid JSON)
    # share one overall per-unit budget, so they cannot multiply into a long retry loop.
    gemini_max_attempts: int = field(default_factory=lambda: max(1, min(2, _int("GEMINI_MAX_ATTEMPTS", 2))))
    llm_max_repair_attempts: int = field(default_factory=lambda: max(0, min(2, _int("LLM_MAX_REPAIR_ATTEMPTS", 2))))
    # Wall-clock budget for all Gemini attempts of one unit (backoff / Retry-After never exceed it).
    gemini_total_budget_sec: int = field(default_factory=lambda: _int("GEMINI_TOTAL_BUDGET_SECONDS", 300))
    gemini_max_retry_after_sec: int = field(default_factory=lambda: _int("GEMINI_MAX_RETRY_AFTER_SECONDS", 20))
    # Circuit breaker: after this many consecutive eligible Gemini failures (any job), skip Gemini for the
    # cooldown. Auth failures and exhausted quota open it at once. Reset by success, cooldown or a new key/model.
    llm_circuit_failure_threshold: int = field(default_factory=lambda: max(1, _int("LLM_CIRCUIT_FAILURE_THRESHOLD", 2)))
    llm_circuit_cooldown_sec: int = field(default_factory=lambda: _int("LLM_CIRCUIT_COOLDOWN_SECONDS", 600))
    # Long videos: from this transcript length Gemini also writes in validated stages (part summaries -> outline ->
    # slide batches), each stage falling back to Ollama on Modal on its own (app/ai/staged.py).
    llm_staged_min_sec: int = field(default_factory=lambda: _int("LLM_STAGED_MIN_SECONDS", 900))
    # Context window for the staged route (both providers; Modal clamps it to the worker's max_num_ctx).
    llm_long_num_ctx: int = field(default_factory=lambda: max(4096, min(32768, _int("LLM_LONG_NUM_CTX", 32768))))

    # Ollama on Modal (GPU worker infrastructure/modal_llm_worker.py), called through its proxy-auth
    # HTTPS API with the same MODAL_PROXY_TOKEN_* as the image worker. The Modal-side limits
    # (GPU, max containers, scale-down) are set in the worker file at deploy time; the values here
    # are what the backend expects and reports.
    modal_llm_worker_url: str = field(default_factory=lambda: os.environ.get("MODAL_LLM_WORKER_URL", "").rstrip("/"))
    modal_llm_app_name: str = field(default_factory=lambda: os.environ.get("MODAL_LLM_APP_NAME", "video2slides-llm-worker"))
    modal_llm_model: str = field(default_factory=lambda: os.environ.get("MODAL_LLM_MODEL", "qwen3:4b"))
    modal_llm_gpu: str = field(default_factory=lambda: os.environ.get("MODAL_LLM_GPU", "L4"))
    # Context window on the Modal GPU. 8k forced a 120-min transcript down to ~2k tokens of notes for the
    # outline; qwen3:4b's KV cache at 32k is ~5 GB, which fits next to the weights on L4/A10G/T4. Clamped to
    # the worker's advertised max_num_ctx, so an older worker deployment keeps working.
    modal_llm_num_ctx: int = field(default_factory=lambda: max(4096, min(32768, _int("MODAL_LLM_NUM_CTX", 32768))))
    # One chat request, including a cold start (container + Ollama start + model load).
    modal_llm_timeout_sec: int = field(default_factory=lambda: _int("MODAL_LLM_REQUEST_TIMEOUT_SECONDS", 600))
    modal_http_timeout_sec: int = field(default_factory=lambda: _int("MODAL_HTTP_TIMEOUT_SECONDS", 30))
    modal_poll_interval_sec: float = field(default_factory=lambda: _float("MODAL_POLL_INTERVAL_SECONDS", 1.5))
    modal_gpu_price_per_sec: float = field(default_factory=lambda: _float("MODAL_GPU_PRICE_PER_SEC", 0.000222))

    # Video processing: modal = FFmpeg (CPU) + faster-whisper (GPU) on Modal (infrastructure/modal_media_worker.py);
    # local = FFmpeg + faster-whisper on this computer; auto = modal when MODAL_MEDIA_WORKER_URL is set.
    media_processing: str = field(default_factory=lambda: os.environ.get("MEDIA_PROCESSING", "auto").strip().lower())
    modal_media_worker_url: str = field(default_factory=lambda: os.environ.get("MODAL_MEDIA_WORKER_URL", "").rstrip("/"))
    modal_media_upload_chunk_mb: int = field(default_factory=lambda: max(1, min(32, _int("MODAL_MEDIA_UPLOAD_CHUNK_MB", 4))))
    modal_media_timeout_sec: int = field(default_factory=lambda: _int("MODAL_MEDIA_TIMEOUT_SECONDS", 2400))
    # Whisper on the GPU worker. Measured on L4 (scripts/asr_eval.py, Vietnamese lecture clip):
    #   base sequential 22.1% syllable errors, small sequential 13.5%, small batched (16) + beam 5 + word
    #   timestamps 12.6% and 2-3x faster. float16; int8_float16 only after measuring quality.
    modal_whisper_model: str = field(default_factory=lambda: os.environ.get("MODAL_WHISPER_MODEL", "small"))
    # English videos (scripts/asr_benchmark_en.py, LibriSpeech, batched 16 + beam 5 on L4):
    #   small 1.94/5.27% WER (clean/other) | large-v3-turbo 1.33/3.34%, 90 min in 63 s | large-v3 1.25/2.74%, 118 s
    modal_whisper_model_en: str = field(default_factory=lambda: os.environ.get("MODAL_WHISPER_MODEL_EN", "large-v3-turbo"))
    modal_whisper_compute_type: str = field(default_factory=lambda: os.environ.get("MODAL_WHISPER_COMPUTE_TYPE", "float16"))
    modal_whisper_beam_size: int = field(default_factory=lambda: max(1, min(5, _int("MODAL_WHISPER_BEAM_SIZE", 5))))
    # 0 = sequential decoding; > 0 = faster-whisper BatchedInferencePipeline (text decoded without timestamp
    # tokens, sentence segments rebuilt from word timestamps).
    modal_whisper_batch_size: int = field(default_factory=lambda: max(0, min(32, _int("MODAL_WHISPER_BATCH_SIZE", 16))))
    # Send only the audio track (remuxed locally with PyAV, no re-encoding) + a few frames instead of the video.
    modal_upload_audio_only: bool = field(default_factory=lambda: _bool("MODAL_UPLOAD_AUDIO_ONLY", True))

    # Local LLM through Ollama's native API. These are mapped to /api/chat request fields
    # (options.num_ctx, options.temperature, keep_alive, think); Ollama does not read these names itself.
    ollama_base_url: str = field(default_factory=lambda: os.environ.get(
        "OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"))
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen3:4b"))
    ollama_num_ctx: int = field(default_factory=lambda: max(2048, _int("OLLAMA_NUM_CTX", 8192)))
    ollama_temperature: float = field(default_factory=lambda: _float("OLLAMA_TEMPERATURE", 0.2))
    ollama_keep_alive: str = field(default_factory=lambda: os.environ.get("OLLAMA_KEEP_ALIVE", "5m"))
    ollama_timeout_sec: int = field(default_factory=lambda: _int("OLLAMA_REQUEST_TIMEOUT_SECONDS", 600))
    # off | on | auto. auto = disable thinking when the model reports the capability (faster, JSON only).
    ollama_think: str = field(default_factory=lambda: os.environ.get("OLLAMA_THINK", "auto").strip().lower())
    # Upper bound for slides written per request (smaller = less output per call, more calls).
    ollama_slides_per_batch: int = field(default_factory=lambda: max(1, min(6, _int("OLLAMA_SLIDES_PER_BATCH", 3))))
    # Repair attempts after an invalid answer (0-2; the first attempt is not counted).
    ollama_max_repairs: int = field(default_factory=lambda: max(0, min(2, _int("OLLAMA_MAX_REPAIRS", 2))))
    # Free the Whisper model before LLM inference (it is reloaded from disk for the next job).
    release_whisper_before_llm: bool = field(default_factory=lambda: _bool("RELEASE_WHISPER_BEFORE_LLM", True))
    gemini_api_key: str = field(default_factory=lambda: os.environ.get("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
    # Comma-separated models tried in order when the main model is overloaded (HTTP 503)
    gemini_fallback_models: tuple[str, ...] = field(default_factory=lambda: tuple(
        m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest").split(",") if m.strip()))
    # GEMINI_REQUEST_TIMEOUT_SECONDS (older name: AI_TIMEOUT_SEC), per HTTP request
    ai_timeout_sec: int = field(default_factory=lambda: _int(
        "GEMINI_REQUEST_TIMEOUT_SECONDS", _int("AI_TIMEOUT_SEC", 120)))
    ai_max_attempts: int = field(default_factory=lambda: _int("AI_MAX_ATTEMPTS", 3))

    # Access control: when set, every API call must send this shared password.
    app_password: str = field(default_factory=lambda: os.environ.get("APP_PASSWORD", ""))
    # Serverless hosting (Modal) stops an idle container even while the job thread is working: while a job
    # is queued or running, the app requests its own /api/health every KEEPALIVE_INTERVAL_SECONDS.
    keepalive_while_busy: bool = field(default_factory=lambda: _bool("KEEPALIVE_WHILE_BUSY", False))
    keepalive_interval_sec: int = field(default_factory=lambda: max(10, _int("KEEPALIVE_INTERVAL_SECONDS", 60)))

    # Video URL import (YouTube / Facebook)
    url_import_enabled: bool = field(default_factory=lambda: _bool("URL_IMPORT_ENABLED", True))
    # off | ytdlp. Off by default: YouTube's Terms do not authorise third-party downloading.
    youtube_retrieval: str = field(default_factory=lambda: os.environ.get("YOUTUBE_RETRIEVAL", "off").lower())
    # off | graph_api | ytdlp. graph_api only works for videos the token may read (e.g. your own Page).
    facebook_retrieval: str = field(default_factory=lambda: os.environ.get("FACEBOOK_RETRIEVAL", "graph_api").lower())
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    facebook_access_token: str = field(default_factory=lambda: os.environ.get("FACEBOOK_ACCESS_TOKEN", ""))
    facebook_graph_version: str = field(default_factory=lambda: os.environ.get("FACEBOOK_GRAPH_VERSION", "v23.0"))
    url_max_duration_sec: int = field(default_factory=lambda: _int("URL_MAX_DURATION_SEC", 7200))
    url_max_download_mb: int = field(default_factory=lambda: _int("URL_MAX_DOWNLOAD_MB", 1536))
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

    # Kept for compatibility: slides are always full width, explanations go to the speaker notes
    default_layout_mode: str = field(default_factory=lambda: os.environ.get("DEFAULT_LAYOUT_MODE", "presentation").lower())

    # Google Slides output (OAuth "Desktop app" client; the JSON is downloaded from Google Cloud Console)
    google_slides_enabled: bool = field(default_factory=lambda: _bool("GOOGLE_SLIDES_ENABLED", True))
    google_oauth_client_file: Path = field(default_factory=lambda: Path(os.environ.get(
        "GOOGLE_OAUTH_CLIENT_FILE", Path(os.environ.get("DATA_DIR", BASE_DIR / "data")) / "google" / "client_secret.json")))
    google_http_timeout_sec: int = field(default_factory=lambda: _int("GOOGLE_HTTP_TIMEOUT_SEC", 120))
    google_max_attempts: int = field(default_factory=lambda: max(1, _int("GOOGLE_MAX_ATTEMPTS", 4)))

    @property
    def media_on_modal(self) -> bool:
        mode = self.media_processing
        return mode == "modal" or (mode == "auto" and bool(self.modal_media_worker_url))

    @property
    def google_dir(self) -> Path:
        """Tokens live outside the job folders, so job cleanup never deletes them."""
        return self.data_dir / "google"

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
