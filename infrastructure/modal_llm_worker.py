"""Private Ollama (qwen3:4b) LLM worker for Video2Slides on Modal serverless GPUs.

Deploy (administrator only, see docs/MODAL_CLOUD_SETUP_VI.md):

    modal deploy infrastructure/modal_llm_worker.py
    modal run infrastructure/modal_llm_worker.py::setup_model     # one-time model download, CPU only

Architecture (separate from the SDXL image worker: own app, own GPU allocation, own Volume)
* ``OllamaWorker`` (GPU: L4, else A10G/T4): starts ONE ``ollama serve`` per container, bound to 127.0.0.1 only
  (port 11434 is never exposed), waits for it with a bounded timeout, checks the model files are complete,
  loads the model and verifies with ``/api/ps`` that the weights are in GPU memory. CPU-only inference is
  refused (``gpu_not_used``), never silently accepted. ``max_containers=1`` + one input per container =
  at most one active generation; ``min_containers=0`` + ``scaledown_window`` = scales to zero when idle.
* ``api`` (CPU): FastAPI app protected by Modal **proxy auth** (``Modal-Key`` / ``Modal-Secret`` headers of a
  workspace proxy token, rejected by Modal with 401 otherwise). It validates and size-limits each chat request,
  spawns it on the GPU class and returns results by polling. ``/v1/health`` reads the model marker from the
  Volume and never starts a GPU.
* Model files live in a Modal Volume (``OLLAMA_MODELS``). ``setup_model`` downloads them once on a CPU
  container (``max_containers=1``: no concurrent downloads) and writes a ready-marker only after Ollama
  reports success, so a partial download is never treated as ready. GPU containers only read the Volume.

Application settings -> Ollama parameters (Ollama itself does not read the app's .env names):
  OLLAMA_NUM_CTX -> options.num_ctx, OLLAMA_TEMPERATURE -> options.temperature, OLLAMA_KEEP_ALIVE -> keep_alive,
  OLLAMA_THINK -> think, output budget -> options.num_predict, JSON schema -> format.
Ollama keep_alive only keeps the model loaded INSIDE a running container; Modal ``scaledown_window`` decides
how long the (billed) GPU container itself stays up after the last request.

Prompts contain user transcripts: they are never logged. Keep this file standalone (deployed on its own) and do
not add ``from __future__ import annotations`` (FastAPI could not resolve the request models, see
tests/test_modal_worker.py).
"""

import asyncio
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

import modal

APP_NAME = os.environ.get("V2S_LLM_APP_NAME", "video2slides-llm-worker")
OLLAMA_VERSION = "0.40.3"           # pinned; tested with qwen3:4b (think=false supported)
MODEL = os.environ.get("V2S_LLM_MODEL", "qwen3:4b")
# Fallback order when L4 has no free capacity (qwen3:4b needs ~4 GB VRAM; all three fit it fully).
GPU_TYPES = [g.strip() for g in os.environ.get("V2S_LLM_GPU", "L4,A10G,T4").split(",") if g.strip()]
MODELS_DIR = "/models"
OLLAMA_ADDR = "127.0.0.1:11434"     # loopback only: the daemon is reachable from inside the container only
OLLAMA_URL = f"http://{OLLAMA_ADDR}"
OLLAMA_LOG = "/tmp/ollama.log"     # container-local; never contains prompts (Ollama logs request metadata only)
DEFAULT_NUM_CTX = 8192              # the warm-up load uses the same context as requests (no reload per request)
SCALEDOWN_SEC = int(os.environ.get("V2S_LLM_SCALEDOWN_SEC", "60"))
REQUEST_TIMEOUT_SEC = 600           # one generation, including a cold start
STARTUP_TIMEOUT_SEC = 60            # `ollama serve` must answer within this
LOAD_TIMEOUT_SEC = 240              # model load into GPU memory

# Request limits (checked in the CPU API before any GPU starts, and again on the GPU side)
MAX_REQUEST_BYTES = 256 * 1024
MAX_MESSAGES = 8
MAX_NUM_CTX = 16384
MAX_NUM_PREDICT = 8192
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
CALL_ID_RE = re.compile(r"^fc-[A-Za-z0-9]{10,64}$")


def marker_path(model: str = MODEL) -> str:
    return f"{MODELS_DIR}/.v2s-ready-{re.sub(r'[^A-Za-z0-9_.-]', '_', model)}.json"


gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("curl", "ca-certificates", "zstd")
    .run_commands(
        f"curl -fsSL -o /tmp/ollama.tar.zst "
        f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/ollama-linux-amd64.tar.zst",
        "tar --zstd -xf /tmp/ollama.tar.zst -C /usr && rm /tmp/ollama.tar.zst",
        "ollama --version || true",
    )
    .env({
        "OLLAMA_MODELS": MODELS_DIR,
        "OLLAMA_HOST": OLLAMA_ADDR,
        "OLLAMA_NUM_PARALLEL": "1",        # one generation per container
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_CONTEXT_LENGTH": str(DEFAULT_NUM_CTX),
        "OLLAMA_KEEP_ALIVE": "5m",
        "OLLAMA_NOPRUNE": "1",              # never rewrite the shared model Volume at startup
    })
)
web_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]==0.115.6")

app = modal.App(APP_NAME)
models_volume = modal.Volume.from_name("video2slides-ollama-models", create_if_missing=True)
request_index = modal.Dict.from_name("video2slides-llm-requests", create_if_missing=True)


# ---------------------------------------------------------------------- shared helpers

def validate_request(req: dict) -> dict:
    """Validate and normalise one chat request (raises ValueError). Only whitelisted fields reach Ollama."""
    if not isinstance(req, dict):
        raise ValueError("request must be an object")
    if len(json.dumps(req, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    if req.get("model") != MODEL:
        raise ValueError(f"only model {MODEL} is served")
    messages = req.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("invalid messages")
    clean = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") \
                or not isinstance(m.get("content"), str):
            raise ValueError("invalid message")
        clean.append({"role": m["role"], "content": m["content"]})
    opts = req.get("options") or {}
    num_ctx = int(opts.get("num_ctx", DEFAULT_NUM_CTX))
    temperature = float(opts.get("temperature", 0.2))
    if not 2048 <= num_ctx <= MAX_NUM_CTX:
        raise ValueError("invalid num_ctx")
    if not 0 <= temperature <= 1.5:
        raise ValueError("invalid temperature")
    options = {"num_ctx": num_ctx, "temperature": temperature}
    if opts.get("num_predict") is not None:
        num_predict = int(opts["num_predict"])
        if not 1 <= num_predict <= MAX_NUM_PREDICT:
            raise ValueError("invalid num_predict")
        options["num_predict"] = num_predict
    fmt = req.get("format")
    if fmt is not None and not (fmt == "json" or isinstance(fmt, dict)):
        raise ValueError("invalid format")
    think = req.get("think")
    if think is not None and not isinstance(think, bool):
        raise ValueError("invalid think")
    keep_alive = str(req.get("keep_alive") or "5m")
    if not re.match(r"^(0|\d{1,4}[smh])$", keep_alive):
        raise ValueError("invalid keep_alive")
    out = {"model": MODEL, "messages": clean, "stream": False, "options": options, "keep_alive": keep_alive}
    if fmt is not None:
        out["format"] = fmt
    if think is not None:
        out["think"] = think
    return out


def _http(method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(f"{OLLAMA_URL}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _start_ollama() -> subprocess.Popen:
    # Ollama logs every request to stderr. It must go to a file: an unread PIPE fills up (~64 KB) after a few dozen
    # requests and then blocks the server on its next log write (observed: long decks hung after ~30 requests).
    log = open(OLLAMA_LOG, "ab")
    proc = subprocess.Popen(["ollama", "serve"], stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("ollama serve exited during startup")
        try:
            _http("GET", "/api/version", timeout=2)
            return proc
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError(f"ollama serve not ready after {STARTUP_TIMEOUT_SEC}s")


def _read_marker(model: str = MODEL) -> dict | None:
    try:
        with open(marker_path(model), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _prefetch_blobs() -> int:
    """Read the model files once, sequentially, before Ollama mmaps them: random page faults over the network
    Volume are much slower than one sequential read that leaves the data in the page cache."""
    total = 0
    blob_dir = f"{MODELS_DIR}/blobs"
    for name in sorted(os.listdir(blob_dir)) if os.path.isdir(blob_dir) else []:
        path = os.path.join(blob_dir, name)
        if os.path.getsize(path) < 1 << 20 or name.endswith("-partial"):
            continue
        with open(path, "rb", buffering=0) as fh:
            while chunk := fh.read(16 << 20):
                total += len(chunk)
    return total


STALL_SEC = 60            # no new token for this long -> abort (closing the stream stops Ollama generating)
MIN_TOKENS_PER_SEC = 20   # wall-time budget (measured ~70 tok/s on L4): prompt time + num_predict at this (very low) rate + margin


class _Stalled(Exception):
    pass


def _print_log_tail(n: int = 4000) -> None:
    try:
        with open(OLLAMA_LOG, "rb") as fh:
            fh.seek(max(0, os.path.getsize(OLLAMA_LOG) - n))
            print("ollama log tail: " + fh.read().decode("utf-8", "replace"))
    except OSError:
        pass


def _stream_chat(body: dict) -> dict:
    """/api/chat streamed, so a stuck or pathologically slow generation is detected and stopped early instead of
    holding the GPU until the function timeout. Logs token progress (counts only, never content)."""
    num_predict = int((body.get("options") or {}).get("num_predict") or 4096)
    budget = min(REQUEST_TIMEOUT_SEC - 40, 90 + num_predict / MIN_TOKENS_PER_SEC)
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps({**body, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    content, thinking, final = [], [], {}
    t0 = last_token = last_log = time.monotonic()
    chunks = 0
    try:
        resp = urllib.request.urlopen(req, timeout=STALL_SEC)  # also the per-read timeout: no token -> TimeoutError
    except TimeoutError:
        _print_log_tail()
        raise _Stalled(f"Ollama không bắt đầu trả lời trong {STALL_SEC} s.") from None
    with resp:
        lines = iter(resp)
        while True:
            try:
                line = next(lines)
            except StopIteration:
                break
            except TimeoutError:
                _print_log_tail()
                raise _Stalled(f"Không có token mới trong {STALL_SEC} s (đã sinh {chunks} token).") from None
            now = time.monotonic()
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(str(msg["error"])[:200])
            m = msg.get("message") or {}
            if m.get("content") or m.get("thinking"):
                chunks += 1
                last_token = now
                content.append(m.get("content") or "")
                thinking.append(m.get("thinking") or "")
            if msg.get("done"):
                final = msg
                break
            if now - last_log >= 30:
                last_log = now
                print(f"  generating: {chunks} tokens in {now - t0:.0f}s ({chunks / (now - t0):.1f}/s)")
            if now - last_token > STALL_SEC:
                _print_log_tail()
                raise _Stalled(f"Không có token mới trong {STALL_SEC} s (đã sinh {chunks} token).")
            if now - t0 > budget:
                raise _Stalled(f"Sinh {chunks} token trong {now - t0:.0f} s, quá ngân sách {budget:.0f} s "
                               f"cho num_predict={num_predict}.")
    if not final:
        raise _Stalled("Ollama đóng luồng trước khi kết thúc câu trả lời.")
    final["message"] = {"role": "assistant", "content": "".join(content), "thinking": "".join(thinking)}
    return final


def _nvidia_smi() -> dict:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        name, used, total, util = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return {"gpu": name, "vram_used_mb": int(used), "vram_total_mb": int(total), "gpu_util_pct": int(util)}
    except Exception:
        return {}


def _loaded() -> dict:
    """The served model as reported by /api/ps (size_vram ~ size means it is fully in GPU memory)."""
    for m in _http("GET", "/api/ps").get("models", []):
        if (m.get("name") or m.get("model")) == MODEL:
            size, vram = int(m.get("size") or 0), int(m.get("size_vram") or 0)
            return {"size_mb": round(size / 2**20), "size_vram_mb": round(vram / 2**20),
                    "gpu_fraction": round(vram / size, 3) if size else 0.0,
                    "context_length": m.get("context_length")}
    return {}


# ---------------------------------------------------------------------- one-time setup (CPU)

@app.function(image=gpu_image, volumes={MODELS_DIR: models_volume}, timeout=1800, cpu=2.0, memory=4096,
              max_containers=1)
def setup_model(model: str = MODEL) -> dict:
    """Download the model once into the Volume (CPU container: no GPU billed for the download).

    Re-running is cheap: complete files are reused. The ready-marker is written only after Ollama reports
    success and the model shows up in /api/tags, then the Volume is committed.
    """
    if model != MODEL:
        raise ValueError(f"this worker serves {MODEL} only (refusing to download {model})")
    proc = _start_ollama()
    try:
        tags = [m.get("name") for m in _http("GET", "/api/tags").get("models", [])]
        marker = _read_marker()
        if model in tags and marker:
            return {"status": "already_ready", **marker}
        print(f"pulling {model} (Ollama {OLLAMA_VERSION}) into the Volume ...")
        t0 = time.monotonic()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/pull", data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        last_pct, status = -10, ""
        with urllib.request.urlopen(req, timeout=1500) as resp:
            for line in resp:
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(f"pull failed: {msg['error']}")
                status = msg.get("status", "")
                if msg.get("total") and msg.get("completed"):
                    pct = int(100 * msg["completed"] / msg["total"])
                    if pct >= last_pct + 10:
                        last_pct = pct
                        print(f"  {status}: {pct}% of {msg['total'] / 1e9:.2f} GB")
                elif status and not status.startswith("pulling "):
                    print(f"  {status}")
        if status != "success":
            raise RuntimeError(f"pull did not finish (last status: {status!r})")
        tags = {m.get("name"): m for m in _http("GET", "/api/tags").get("models", [])}
        if model not in tags:
            raise RuntimeError("model missing after pull")
        info = _http("POST", "/api/show", {"model": model}, timeout=30)
        details = info.get("details") or {}
        ctx = next((int(v) for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
        marker = {
            "model": model, "ollama_version": OLLAMA_VERSION, "digest": tags[model].get("digest"),
            "size_bytes": tags[model].get("size"), "capabilities": info.get("capabilities") or [],
            "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
            "family": details.get("family"), "context_length": ctx,
            "download_seconds": round(time.monotonic() - t0, 1), "ready_at": time.time(),
        }
        tmp = marker_path(model) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
        os.replace(tmp, marker_path(model))
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    models_volume.commit()
    return {"status": "downloaded", **marker}


# ---------------------------------------------------------------------- GPU worker

@app.cls(
    image=gpu_image,
    gpu=GPU_TYPES,
    volumes={MODELS_DIR: models_volume},
    min_containers=0,                 # scale to zero: no GPU billed while idle
    max_containers=1,                 # at most one LLM GPU container
    scaledown_window=SCALEDOWN_SEC,   # warm period after the last request (billed)
    timeout=REQUEST_TIMEOUT_SEC,
    startup_timeout=STARTUP_TIMEOUT_SEC + LOAD_TIMEOUT_SEC,
    retries=0,                        # retries are decided by the backend (bounded)
)
@modal.concurrent(max_inputs=1)       # one active generation per container
class OllamaWorker:
    @modal.enter()
    def start(self) -> None:
        t0 = time.monotonic()
        self.first_call = True
        self.startup_error = None
        self.startup = {}
        self.proc = None
        marker = _read_marker()
        if not marker:
            self.startup_error = ("model_missing", f"Model {MODEL} chưa được tải vào Volume. "
                                  "Chạy: modal run infrastructure/modal_llm_worker.py::setup_model")
            return
        try:
            self.proc = _start_ollama()
            served = _http("GET", "/api/version").get("version", "")
            tags = [m.get("name") for m in _http("GET", "/api/tags").get("models", [])]
            if MODEL not in tags:
                self.startup_error = ("model_missing", f"Volume có marker nhưng Ollama không thấy {MODEL}; "
                                                       "chạy lại setup_model.")
                return
            t_read = time.monotonic()
            read_bytes = _prefetch_blobs()
            t_load = time.monotonic()
            # Load the weights now (same num_ctx as requests, so the first request does not reload).
            _http("POST", "/api/generate", {"model": MODEL, "prompt": "", "keep_alive": "5m", "stream": False,
                                            "options": {"num_ctx": DEFAULT_NUM_CTX}}, timeout=LOAD_TIMEOUT_SEC)
            t_warm = time.monotonic()
            # Measured: the first generation after a load is ~30 s slower (GPU kernels warm-up); pay it here, so
            # it overlaps with transcription when the backend pre-warms this container.
            _http("POST", "/api/generate", {"model": MODEL, "prompt": "Xin chào", "keep_alive": "5m", "think": False, "stream": False,
                                            "options": {"num_ctx": DEFAULT_NUM_CTX, "num_predict": 1}},
                  timeout=LOAD_TIMEOUT_SEC)
            loaded = _loaded()
            self.startup = {"ollama_version": served, "ollama_start_seconds": round(t_read - t0, 2),
                            "prefetch_seconds": round(t_load - t_read, 2), "prefetch_mb": round(read_bytes / 2**20),
                            "model_load_seconds": round(t_warm - t_load, 2),
                            "warmup_generation_seconds": round(time.monotonic() - t_warm, 2), **_nvidia_smi(), **loaded}
            if not loaded or loaded["gpu_fraction"] < 0.95:
                # Refuse CPU (or partial) offload in a GPU worker instead of running slowly on CPU.
                self.startup_error = ("gpu_not_used", f"Ollama không nạp model lên GPU (size_vram/size = "
                                                      f"{loaded.get('gpu_fraction', 0)}).")
        except Exception as exc:  # details stay in Modal logs; never prompt content
            print(f"startup failed: {exc.__class__.__name__}: {exc}")
            self.startup_error = ("startup_failed", f"Không khởi động được Ollama trên GPU ({exc.__class__.__name__}).")
        self.startup["startup_seconds"] = round(time.monotonic() - t0, 2)

    @modal.exit()
    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    @modal.method()
    def warmup(self) -> dict:
        """Start the container (and load the model) ahead of the first real request."""
        return {"ok": not self.startup_error, "startup": self.startup}

    @modal.method()
    def generate_content(self, request: dict) -> dict:
        cold, self.first_call = self.first_call, False
        if self.startup_error:
            code, message = self.startup_error
            return {"ok": False, "error_code": code, "error": message, "retryable": False,
                    "metrics": {"cold_start": cold, **self.startup}}
        try:
            body = validate_request(request)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error_code": "invalid_request", "error": str(exc), "retryable": False}
        t0 = time.monotonic()
        try:
            out = _stream_chat(body)
        except _Stalled as exc:
            return {"ok": False, "error_code": "generation_stalled", "error": str(exc), "retryable": True}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "error_code": "ollama_error", "error": f"Ollama HTTP {exc.code}: {text}",
                    "retryable": exc.code >= 500}
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return {"ok": False, "error_code": "ollama_unavailable",
                    "error": f"Ollama trong container không phản hồi ({exc.__class__.__name__}).", "retryable": True}
        wall = time.monotonic() - t0
        loaded = _loaded()
        smi = _nvidia_smi()
        if loaded and loaded["gpu_fraction"] < 0.95:
            return {"ok": False, "error_code": "gpu_not_used", "retryable": False,
                    "error": "Model không còn nằm hoàn toàn trên GPU; từ chối kết quả chạy bằng CPU."}
        msg = out.get("message") or {}
        ns = 1e9
        metrics = {
            "cold_start": cold, "wall_seconds": round(wall, 2), **loaded, **smi,
            **(self.startup if cold else {}),
        }
        print(f"chat ok: prompt={out.get('prompt_eval_count')} out={out.get('eval_count')} "
              f"{wall:.1f}s cold={cold} gpu_fraction={loaded.get('gpu_fraction')}")
        return {
            "ok": True, "content": msg.get("content") or "", "thinking": msg.get("thinking") or "",
            "done_reason": out.get("done_reason") or "",
            "prompt_eval_count": out.get("prompt_eval_count") or 0, "eval_count": out.get("eval_count") or 0,
            "load_sec": (out.get("load_duration") or 0) / ns, "total_sec": (out.get("total_duration") or 0) / ns,
            "eval_sec": (out.get("eval_duration") or 0) / ns,
            "prompt_eval_sec": (out.get("prompt_eval_duration") or 0) / ns,
            "model": MODEL, "ollama_version": self.startup.get("ollama_version", OLLAMA_VERSION),
            "metrics": metrics,
        }


# ---------------------------------------------------------------------- authenticated CPU API

@app.function(image=web_image, volumes={MODELS_DIR: models_volume}, min_containers=0, max_containers=1,
              scaledown_window=60, timeout=60)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from fastapi import FastAPI, HTTPException, Request

    web = FastAPI(title="Video2Slides LLM worker", docs_url=None, redoc_url=None, openapi_url=None)

    @web.get("/v1/health")
    def health() -> dict:
        """Deployment + model-file readiness. Never starts a GPU (a warm/cold GPU is unknown here)."""
        try:
            models_volume.reload()
        except Exception:
            pass
        marker = _read_marker()
        return {"ok": True, "app": APP_NAME, "model": MODEL, "ollama_version": OLLAMA_VERSION, "gpu": GPU_TYPES,
                "model_ready": bool(marker), "model_info": marker or {},
                "limits": {"max_containers": 1, "max_inputs_per_container": 1, "scaledown_window_sec": SCALEDOWN_SEC,
                           "request_timeout_sec": REQUEST_TIMEOUT_SEC, "max_request_bytes": MAX_REQUEST_BYTES,
                           "max_num_ctx": MAX_NUM_CTX}}

    @web.post("/v1/warmup")
    def warmup() -> dict:
        """Pre-warm the single GPU container (the backend calls this only when the job will use it)."""
        call = OllamaWorker().warmup.spawn()
        return {"call_id": call.object_id}

    @web.post("/v1/chat")
    async def chat(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_REQUEST_BYTES + 4096:
            raise HTTPException(status_code=413, detail="request too large")
        try:
            body = json.loads(raw)
            request_id = str(body.get("request_id") or "")
            params = validate_request(body.get("request"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:200]) from None
        if not REQUEST_ID_RE.match(request_id):
            raise HTTPException(status_code=422, detail="invalid request_id")
        # Blocking Modal calls run in a worker thread, never on the event loop that also serves result polls.
        return await asyncio.to_thread(_submit, request_id, params)

    def _submit(request_id: str, params: dict) -> dict:
        existing = request_index.get(request_id)
        if existing:
            return {"call_id": existing, "deduplicated": True}
        call = OllamaWorker().generate_content.spawn(params)
        if not request_index.put(request_id, call.object_id, skip_if_exists=True):
            call.cancel()
            return {"call_id": request_index.get(request_id), "deduplicated": True}
        return {"call_id": call.object_id, "deduplicated": False}

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
            return {"status": "failed", "error_code": "timeout", "error": "Quá thời gian chạy trên GPU.",
                    "retryable": True}
        except modal.exception.InputCancellation:
            return {"status": "failed", "error_code": "cancelled", "error": "Đã hủy.", "retryable": False}
        except modal.exception.NotFoundError:
            raise HTTPException(status_code=404, detail="unknown call") from None
        except Exception as exc:  # container crash etc.; details stay in Modal logs
            return {"status": "failed", "error_code": "worker_error",
                    "error": f"LLM worker lỗi ({exc.__class__.__name__}).", "retryable": True}
        if not out.get("ok"):
            return {"status": "failed", **{k: out.get(k) for k in ("error_code", "error", "retryable", "metrics")}}
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

    return web


@app.local_entrypoint()
def smoke_test():
    """One tiny structured Vietnamese request directly on the GPU class (bypasses the web API)."""
    schema = {"type": "object", "properties": {"tieu_de": {"type": "string"}, "y_chinh": {
        "type": "array", "items": {"type": "string"}}}, "required": ["tieu_de", "y_chinh"]}
    t0 = time.perf_counter()
    out = OllamaWorker().generate_content.remote({
        "model": MODEL, "think": False, "format": schema, "options": {"num_ctx": DEFAULT_NUM_CTX, "num_predict": 300},
        "messages": [{"role": "user", "content": "Tóm tắt thành tiêu đề và 3 ý chính: Nước sôi ở 100 độ C "
                                                 "ở áp suất tiêu chuẩn; trên núi cao nước sôi ở nhiệt độ thấp hơn."}],
    })
    print(json.dumps({k: v for k, v in out.items() if k != "thinking"}, ensure_ascii=False, indent=2))
    print(f"wall {time.perf_counter() - t0:.1f}s")
