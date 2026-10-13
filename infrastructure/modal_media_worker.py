"""Private video-processing worker for Video2Slides on Modal: FFmpeg (CPU) + faster-whisper (GPU).

Deploy (administrator only, see docs/MODAL_CLOUD_SETUP_VI.md):

    modal deploy infrastructure/modal_media_worker.py
    modal run infrastructure/modal_media_worker.py::setup_whisper    # optional: pre-fill the model cache (CPU)

Flow (the backend drives every step and keeps a checkpoint after each one):
1. upload    The backend sends the MP4 in chunks to the proxy-auth API, which stores them in a private Volume
             (no public URL; ids are server-generated, no client paths are accepted).
2. preprocess  CPU function: assembles + verifies (size, sha256), validates with ffprobe (MP4, duration,
             audio), extracts 16 kHz mono PCM audio (CPU: audio work never uses the GPU) and grabs candidate
             frames (single-frame seeks; NVDEC would not make this faster, so it is not used). The source is
             deleted once the audio exists. Returns metadata + small JPEG frames, never the audio.
3. transcribe  GPU class (NVIDIA L4): faster-whisper with device="cuda", compute_type="float16". If CUDA is
             unavailable it fails with ``cuda_unavailable``; it never falls back to CPU. Returns timestamped
             segments and runtime evidence (CTranslate2 CUDA device count, GPU name, VRAM used by the model).
4. delete    The backend deletes the upload after success, failure or cancellation; stale uploads older than
             UPLOAD_TTL_SEC are swept on every new upload.

Limits: one preprocessing container and one GPU container at most, one job per container, scale to zero.
Keep this file standalone and without ``from __future__ import annotations`` (FastAPI request models).
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid

import modal

APP_NAME = os.environ.get("V2S_MEDIA_APP_NAME", "video2slides-media-worker")
GPU_TYPE = os.environ.get("V2S_MEDIA_GPU", "L4")
MEDIA_DIR = "/media"
WHISPER_CACHE = "/whisper-cache"
FASTER_WHISPER_VERSION = "1.2.1"
# Larger models are downloaded into the cache Volume only when a request names them.
ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"}
ALLOWED_COMPUTE = {"float16", "int8_float16"}
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # whole-video mode; audio-only uploads of 90 min are ~45 MB
MAX_CHUNK_BYTES = 32 * 1024 * 1024
MAX_DURATION_SEC = 3 * 3600  # the app allows 120 min; margin for the worker
UPLOAD_TTL_SEC = 24 * 3600
SCALEDOWN_SEC = int(os.environ.get("V2S_MEDIA_SCALEDOWN_SEC", "60"))
ID_RE = re.compile(r"^[0-9a-f]{32}$")
CALL_ID_RE = re.compile(r"^fc-[A-Za-z0-9]{10,64}$")
PY_SITE = "/usr/local/lib/python3.11/site-packages"

cpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
)
gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        f"faster-whisper=={FASTER_WHISPER_VERSION}",
        "ctranslate2>=4.5,<5",
        "nvidia-cublas-cu12==12.*",
        "nvidia-cudnn-cu12==9.*",
    )
    # CTranslate2 loads cuBLAS / cuDNN from these wheels (no CUDA toolkit needed in the image).
    .env({"LD_LIBRARY_PATH": f"{PY_SITE}/nvidia/cublas/lib:{PY_SITE}/nvidia/cudnn/lib",
          "HF_HUB_DISABLE_TELEMETRY": "1"})
)
web_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]==0.115.6")

app = modal.App(APP_NAME)
media_volume = modal.Volume.from_name("video2slides-media", create_if_missing=True)
whisper_cache = modal.Volume.from_name("video2slides-whisper-cache", create_if_missing=True)
progress = modal.Dict.from_name("video2slides-media-progress", create_if_missing=True)


def _dir(upload_id: str) -> str:
    if not ID_RE.match(upload_id or ""):
        raise ValueError("invalid upload id")
    return f"{MEDIA_DIR}/up/{upload_id}"


def _sweep_stale() -> int:
    root, removed = f"{MEDIA_DIR}/up", 0
    if not os.path.isdir(root):
        return 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if time.time() - os.path.getmtime(path) > UPLOAD_TTL_SEC:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


class MediaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _run(args, timeout):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffmpeg_timeout", f"{args[0]} xử lý quá thời gian cho phép.") from exc


# ---------------------------------------------------------------------- 2. preprocessing (CPU)

@app.function(image=cpu_image, volumes={MEDIA_DIR: media_volume}, cpu=2.0, memory=2048, timeout=1800,
              max_containers=1, retries=0)
def preprocess(upload_id: str, sha256: str, size: int, chunks: int, params: dict) -> dict:
    t0 = time.monotonic()
    timings = {}
    try:
        d = _dir(upload_id)
        media_volume.reload()
        chunk_dir = f"{d}/chunks"
        src = f"{d}/input.mp4"
        if not os.path.exists(src):
            names = sorted(os.listdir(chunk_dir)) if os.path.isdir(chunk_dir) else []
            if len(names) != chunks:
                raise MediaError("upload_incomplete", f"Thiếu phần tải lên ({len(names)}/{chunks}).")
            digest, total = hashlib.sha256(), 0
            with open(src + ".tmp", "wb") as out:
                for name in names:
                    with open(f"{chunk_dir}/{name}", "rb") as fh:
                        while block := fh.read(1 << 20):
                            digest.update(block)
                            total += len(block)
                            out.write(block)
            if total != size or digest.hexdigest() != sha256:
                os.remove(src + ".tmp")
                raise MediaError("upload_corrupt", "Tệp tải lên Modal bị hỏng (sai kích thước/sha256).")
            os.replace(src + ".tmp", src)
            shutil.rmtree(chunk_dir, ignore_errors=True)
        timings["assemble_sec"] = round(time.monotonic() - t0, 2)

        # ffprobe validation (same rules as the local app/video.py)
        t = time.monotonic()
        res = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", src], 60)
        if res.returncode != 0:
            raise MediaError("invalid_media", "Không đọc được tệp video (tệp hỏng hoặc không phải video hợp lệ).")
        info = json.loads(res.stdout.decode("utf-8", "replace"))
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = info.get("format", {})
        duration = float(fmt.get("duration") or (video or {}).get("duration") or 0)
        max_dur = min(int(params.get("max_duration_sec") or 600), MAX_DURATION_SEC)
        audio_only = bool(params.get("audio_only"))  # the backend sent only the audio track (+ frames itself)
        if video is None and not audio_only:
            raise MediaError("invalid_media", "Tệp không chứa luồng hình ảnh (video stream).")
        if "mp4" not in str(fmt.get("format_name") or ""):
            raise MediaError("invalid_media", "Chỉ hỗ trợ định dạng MP4.")
        if duration <= 0:
            raise MediaError("invalid_media", "Không xác định được thời lượng video.")
        if duration > max_dur:
            raise MediaError("too_long", f"Video dài {duration / 60:.1f} phút, vượt giới hạn {max_dur / 60:.0f} phút.")
        if audio is None:
            raise MediaError("no_audio", "Video không có âm thanh nên không thể chuyển thành văn bản.")
        video = video or {}
        media = {"duration": duration, "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                 "video_codec": str(video.get("codec_name") or "none"), "audio_codec": str(audio.get("codec_name")),
                 "format_name": str(fmt.get("format_name") or ""), "size_bytes": size}
        timings["probe_sec"] = round(time.monotonic() - t, 2)

        # Audio: 16 kHz mono PCM, optional single-pass loudness normalisation. CPU work by nature.
        t = time.monotonic()
        wav = f"{d}/audio.wav"
        filters = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] if params.get("audio_normalize", True) else []
        res = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src, "-vn", *filters,
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], 1500)
        if res.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) == 0:
            raise MediaError("audio_failed", "Không tách được âm thanh từ video.")
        timings["audio_sec"] = round(time.monotonic() - t, 2)

        # Candidate frames (the backend keeps the best, distinct ones with the same rules as before).
        t = time.monotonic()
        n = 0 if audio_only else max(0, min(32, int(params.get("frame_candidates", 16))))
        width = max(320, min(1920, int(params.get("frame_width") or 1280)))
        margin = min(2.0, duration * 0.05)
        span = max(duration - 2 * margin, 0.1)
        frames = []
        for i in range(n):
            ts = margin + span * (i + 0.5) / n
            target = f"{d}/cand_{i:02d}.jpg"
            r = _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{ts:.2f}", "-i", src, "-frames:v", "1",
                      "-vf", f"scale='min({width},iw)':-2", "-q:v", "4", target], 60)
            if r.returncode == 0 and os.path.exists(target):
                with open(target, "rb") as fh:
                    frames.append({"timestamp": round(ts, 2), "jpeg_b64": base64.b64encode(fh.read()).decode()})
                os.remove(target)
        timings["frames_sec"] = round(time.monotonic() - t, 2)

        os.remove(src)  # only the audio is needed from here on; the backend keeps the original video
        media_volume.commit()
        version = _run(["ffmpeg", "-hide_banner", "-version"], 10).stdout.decode("utf-8", "replace").splitlines()
        timings["total_sec"] = round(time.monotonic() - t0, 2)
        return {"ok": True, "media": media, "frames": frames,
                "audio": {"seconds": duration, "bytes": os.path.getsize(wav), "format": "pcm_s16le 16kHz mono"},
                "timings": timings,
                "ffmpeg": {"location": "modal-cpu", "version": version[0] if version else "",
                           "gpu_operations": [], "note": "audio extraction and frame seeks run on CPU"}}
    except MediaError as exc:
        return {"ok": False, "error_code": exc.code, "error": str(exc), "retryable": False}
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_request", "error": str(exc), "retryable": False}


# ---------------------------------------------------------------------- 3. transcription (GPU)

def decode_options(params: dict) -> tuple[dict, int]:
    """Whitelisted faster-whisper decoding options (raises ValueError). Returns (options, batch_size)."""
    beam = int(params.get("beam_size") or 1)
    batch = int(params.get("batch_size") or 0)
    if not 1 <= beam <= 5 or not 0 <= batch <= 32:
        raise ValueError("invalid beam_size/batch_size")
    prompt = str(params.get("initial_prompt") or "")[:600] or None
    hotwords = str(params.get("hotwords") or "")[:300] or None
    vad = bool(params.get("vad", True))
    opts = {"beam_size": beam, "vad_filter": vad, "initial_prompt": prompt, "hotwords": hotwords,
            "condition_on_previous_text": bool(params.get("condition_on_previous_text", False))}
    if vad and params.get("vad_min_silence_ms") is not None:
        ms = int(params["vad_min_silence_ms"])
        if not 100 <= ms <= 3000:
            raise ValueError("invalid vad_min_silence_ms")
        opts["vad_parameters"] = {"min_silence_duration_ms": ms}
    if batch:
        opts.pop("condition_on_previous_text")  # the batched pipeline decodes chunks independently
        # Its default (without_timestamps=True) returns one segment per ~30 s chunk: too coarse for slides.
        opts["without_timestamps"] = not bool(params.get("timestamps", True))
    if params.get("word_timestamps"):
        # Word timings come from cross-attention alignment, so the text can be decoded without timestamp
        # tokens (more accurate in batched mode) and segments are rebuilt from the words (resegment()).
        opts["word_timestamps"] = True
    return opts, batch


def resegment(words: list, max_words: int = 28, gap: float = 0.6) -> list[dict]:
    """Sentence-sized segments from word timings: cut after . ? ! (or , when long), at pauses, or at max_words."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        text = w.word.strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = nxt is not None and nxt.start - w.end >= gap
        end_sentence = text[-1:] in ".?!…" or (text[-1:] in ",;:" and len(cur) >= max_words // 2)
        if nxt is None or pause or end_sentence or len(cur) >= max_words:
            phrase = "".join(x.word for x in cur).strip()
            if phrase:
                out.append({"start": round(float(cur[0].start), 2), "end": round(float(cur[-1].end), 2), "text": phrase})
            cur = []
    return out


def _gpu_memory_used() -> tuple[str, int]:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        name, used = [x.strip() for x in out.split(",")]
        return name, int(used)
    except Exception:
        return "", -1


@app.cls(
    image=gpu_image,
    gpu=GPU_TYPE,
    volumes={MEDIA_DIR: media_volume, WHISPER_CACHE: whisper_cache},
    min_containers=0,
    max_containers=1,
    scaledown_window=SCALEDOWN_SEC,
    timeout=2400,                     # 90 min of audio with a large model, with margin
    startup_timeout=300,
    retries=0,
)
@modal.concurrent(max_inputs=1)
class WhisperWorker:
    @modal.enter()
    def start(self) -> None:
        self.started_at = time.time()
        t0 = time.monotonic()
        import ctranslate2

        self.first_call = True
        self.models = {}
        self.cuda_devices = ctranslate2.get_cuda_device_count()
        self.ct2_version = ctranslate2.__version__
        self.gpu_name, self.vram_idle_mb = _gpu_memory_used()
        self.enter_sec = round(time.monotonic() - t0, 2)

    def _model(self, name: str, compute_type: str):
        key = (name, compute_type)
        if key in self.models:
            return self.models[key], 0.0, 0
        from faster_whisper import WhisperModel

        before = _gpu_memory_used()[1]
        t0 = time.monotonic()
        model = WhisperModel(name, device="cuda", compute_type=compute_type, download_root=WHISPER_CACHE)
        load = time.monotonic() - t0
        whisper_cache.commit()  # persist weights if this container had to download them
        self.models = {key: model}  # one model in GPU memory at a time
        return model, load, _gpu_memory_used()[1] - before

    @modal.method()
    def transcribe(self, upload_id: str, params: dict) -> dict:
        cold, self.first_call = self.first_call, False
        name = str(params.get("model") or "base")
        compute_type = str(params.get("compute_type") or "float16")
        language = params.get("language") or None
        if name not in ALLOWED_MODELS or compute_type not in ALLOWED_COMPUTE:
            return {"ok": False, "error_code": "invalid_request", "error": "model/compute_type không hợp lệ.",
                    "retryable": False}
        if self.cuda_devices < 1:
            return {"ok": False, "error_code": "cuda_unavailable", "retryable": False,
                    "error": "GPU worker không có CUDA khả dụng cho CTranslate2; từ chối chạy Whisper bằng CPU."}
        try:
            wav = f"{_dir(upload_id)}/audio.wav"
        except ValueError as exc:
            return {"ok": False, "error_code": "invalid_request", "error": str(exc), "retryable": False}
        media_volume.reload()
        if not os.path.exists(wav):
            return {"ok": False, "error_code": "audio_missing", "error": "Không còn tệp âm thanh trên Modal.",
                    "retryable": False}
        try:
            model, load_sec, model_vram_mb = self._model(name, compute_type)
        except Exception as exc:  # CUDA/cuDNN mismatch etc.: explicit, never a CPU fallback
            print(f"model load failed: {exc.__class__.__name__}: {exc}")
            return {"ok": False, "error_code": "cuda_unavailable", "retryable": False,
                    "error": f"Không nạp được Whisper lên GPU ({exc.__class__.__name__}: {str(exc)[:160]})."}
        received_at = time.time()
        try:
            opts, batch_size = decode_options(params)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error_code": "invalid_request", "error": str(exc), "retryable": False}
        t0 = time.monotonic()
        segments, last = [], 0.0
        try:
            lang = None if language in (None, "", "auto") else language
            if batch_size:
                from faster_whisper import BatchedInferencePipeline

                it, info = BatchedInferencePipeline(model).transcribe(wav, language=lang, batch_size=batch_size, **opts)
            else:
                it, info = model.transcribe(wav, language=lang, **opts)
            for s in it:
                if opts.get("word_timestamps") and s.words:
                    segments.extend(resegment(list(s.words)))
                elif s.text and s.text.strip():
                    segments.append({"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": s.text.strip()})
                if time.monotonic() - last >= 2:
                    last = time.monotonic()
                    progress[upload_id] = {"done_sec": round(min(s.end, info.duration), 1),
                                           "total_sec": round(info.duration, 1), "at": time.time()}
        except Exception as exc:
            print(f"transcription failed: {exc.__class__.__name__}")
            return {"ok": False, "error_code": "transcription_failed", "retryable": True,
                    "error": f"Nhận dạng giọng nói thất bại ({exc.__class__.__name__})."}
        elapsed = time.monotonic() - t0
        gpu_name, vram_now = _gpu_memory_used()
        try:
            progress.pop(upload_id, None)
        except Exception:
            pass
        device = getattr(getattr(model, "model", None), "device", "")
        return {
            "ok": True, "segments": segments, "language": info.language or (language or ""),
            "language_probability": round(float(info.language_probability or 0), 3), "duration": info.duration,
            "metrics": {
                "device": device, "compute_type": compute_type, "model": name, "gpu": gpu_name or self.gpu_name,
                "cuda_devices": self.cuda_devices, "ctranslate2": self.ct2_version,
                "faster_whisper": FASTER_WHISPER_VERSION, "vram_idle_mb": self.vram_idle_mb,
                "vram_used_mb": vram_now, "model_vram_mb": model_vram_mb, "cold_start": cold,
                "model_load_sec": round(load_sec, 2), "transcribe_sec": round(elapsed, 2),
                "audio_sec": round(info.duration, 1),
                "realtime_factor": round(info.duration / elapsed, 1) if elapsed else None,
                "batch_size": batch_size, "beam_size": opts["beam_size"],
                "container_started_at": round(self.started_at, 2), "call_received_at": round(received_at, 2),
                "enter_sec": self.enter_sec,
            },
        }


@app.function(image=gpu_image, volumes={WHISPER_CACHE: whisper_cache}, cpu=2.0, memory=4096, timeout=900,
              max_containers=1)
def setup_whisper(model: str = "base") -> dict:
    """Download Whisper weights into the cache Volume on CPU (no GPU billed)."""
    if model not in ALLOWED_MODELS:
        raise ValueError("unsupported model")
    from faster_whisper.utils import download_model

    path = download_model(model, cache_dir=WHISPER_CACHE)
    whisper_cache.commit()
    return {"model": model, "path": path}


# ---------------------------------------------------------------------- authenticated CPU API

@app.function(image=web_image, volumes={MEDIA_DIR: media_volume}, min_containers=0, max_containers=1,
              scaledown_window=60, timeout=120)
@modal.concurrent(max_inputs=8)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel

    web = FastAPI(title="Video2Slides media worker", docs_url=None, redoc_url=None, openapi_url=None)

    class CompleteIn(BaseModel):
        sha256: str
        size: int
        chunks: int
        params: dict = {}

    class TranscribeIn(BaseModel):
        upload_id: str
        params: dict = {}

    def checked(upload_id: str) -> str:
        try:
            return _dir(upload_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="unknown upload") from None

    @web.get("/v1/health")
    def health() -> dict:  # never starts a GPU
        return {"ok": True, "app": APP_NAME, "gpu": GPU_TYPE, "faster_whisper": FASTER_WHISPER_VERSION,
                "models": sorted(ALLOWED_MODELS), "limits": {
                    "max_upload_bytes": MAX_UPLOAD_BYTES, "max_chunk_bytes": MAX_CHUNK_BYTES,
                    "max_duration_sec": MAX_DURATION_SEC, "max_containers": 1, "scaledown_window_sec": SCALEDOWN_SEC}}

    @web.post("/v1/uploads")
    def new_upload() -> dict:
        _sweep_stale()
        upload_id = uuid.uuid4().hex
        os.makedirs(f"{_dir(upload_id)}/chunks", exist_ok=True)
        media_volume.commit()
        return {"upload_id": upload_id, "max_chunk_bytes": MAX_CHUNK_BYTES}

    @web.put("/v1/uploads/{upload_id}/chunks/{index}")
    async def put_chunk(upload_id: str, index: int, request: Request) -> dict:
        d = checked(upload_id)
        if not 0 <= index < MAX_UPLOAD_BYTES // (1024 * 1024):
            raise HTTPException(status_code=422, detail="invalid chunk index")
        data = await request.body()
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail="invalid chunk size")
        return await asyncio.to_thread(_store_chunk, d, index, data)  # file I/O + Volume commit off the event loop

    def _store_chunk(d: str, index: int, data: bytes) -> dict:
        if not os.path.isdir(f"{d}/chunks"):
            raise HTTPException(status_code=404, detail="unknown upload")
        used = sum(os.path.getsize(f"{d}/chunks/{n}") for n in os.listdir(f"{d}/chunks"))
        if used + len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="upload too large")
        with open(f"{d}/chunks/{index:05d}", "wb") as fh:
            fh.write(data)
        media_volume.commit()
        return {"index": index, "bytes": len(data)}

    @web.post("/v1/uploads/{upload_id}/complete")
    def complete(upload_id: str, body: CompleteIn) -> dict:
        checked(upload_id)
        if not re.match(r"^[0-9a-f]{64}$", body.sha256) or not 0 < body.size <= MAX_UPLOAD_BYTES \
                or not 0 < body.chunks <= 1000:
            raise HTTPException(status_code=422, detail="invalid upload summary")
        call = preprocess.spawn(upload_id, body.sha256, body.size, body.chunks, body.params)
        return {"call_id": call.object_id}

    @web.post("/v1/transcribe")
    def transcribe(body: TranscribeIn) -> dict:
        checked(body.upload_id)
        call = WhisperWorker().transcribe.spawn(body.upload_id, body.params)
        return {"call_id": call.object_id}

    @web.get("/v1/progress/{upload_id}")
    def get_progress(upload_id: str) -> dict:
        checked(upload_id)
        return progress.get(upload_id) or {}

    @web.get("/v1/result/{call_id}")
    def result(call_id: str) -> dict:
        if not CALL_ID_RE.match(call_id):
            raise HTTPException(status_code=404, detail="unknown call")
        try:
            out = modal.FunctionCall.from_id(call_id).get(timeout=0)
        except (TimeoutError, modal.exception.TimeoutError):
            return {"status": "pending"}
        except modal.exception.OutputExpiredError:
            return {"status": "expired"}
        except modal.exception.FunctionTimeoutError:
            return {"status": "failed", "error_code": "timeout", "error": "Quá thời gian xử lý trên Modal.",
                    "retryable": True}
        except modal.exception.InputCancellation:
            return {"status": "failed", "error_code": "cancelled", "error": "Đã hủy.", "retryable": False}
        except modal.exception.NotFoundError:
            raise HTTPException(status_code=404, detail="unknown call") from None
        except Exception as exc:
            return {"status": "failed", "error_code": "worker_error",
                    "error": f"Media worker lỗi ({exc.__class__.__name__}).", "retryable": True}
        if not out.get("ok"):
            return {"status": "failed", **{k: out.get(k) for k in ("error_code", "error", "retryable")}}
        return {"status": "done", "result": out}

    @web.post("/v1/cancel/{call_id}")
    def cancel(call_id: str) -> dict:
        if not CALL_ID_RE.match(call_id):
            raise HTTPException(status_code=404, detail="unknown call")
        try:
            modal.FunctionCall.from_id(call_id).cancel()
        except modal.exception.NotFoundError:
            raise HTTPException(status_code=404, detail="unknown call") from None
        return {"cancelled": True}

    @web.delete("/v1/uploads/{upload_id}")
    def delete_upload(upload_id: str) -> dict:
        d = checked(upload_id)
        media_volume.reload()
        existed = os.path.isdir(d)
        shutil.rmtree(d, ignore_errors=True)
        media_volume.commit()
        return {"deleted": existed}

    return web
