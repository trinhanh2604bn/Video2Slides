"""Private SDXL illustration worker for Video2Slides, deployed on Modal serverless GPUs.

Deploy (administrator only, see docs/AI_ILLUSTRATIONS_SETUP_VI.md):

    modal run infrastructure/modal_image_worker.py::download_model   # one-time, CPU only
    modal deploy infrastructure/modal_image_worker.py

Architecture
* ``SDXLWorker`` (GPU, NVIDIA L4): loads stabilityai/stable-diffusion-xl-base-1.0 once per
  container in fp16, generates one image per input, sequentially. ``max_containers=1`` means
  at most one GPU is ever running; ``min_containers=0`` + ``scaledown_window`` means it scales
  to zero shortly after a presentation batch, so idle time is not billed.
* ``api`` (CPU, cheap): a small FastAPI app protected by Modal **proxy auth** — every request
  needs ``Modal-Key`` / ``Modal-Secret`` headers of a workspace proxy token, otherwise Modal
  answers 401 before our code runs. There is no unauthenticated GPU endpoint.
  It only spawns GPU calls and returns their results; it never blocks on inference.
* Weights are cached in a Modal Volume, so they are downloaded once, not per cold start.
* Prompts are derived from user transcripts: they are never logged.

This file is standalone (it is deployed to Modal on its own) and intentionally duplicates
the parameter limits from ``app/illustrations/validation.py``.

Do not add ``from __future__ import annotations``: FastAPI could then not resolve the request
model defined inside ``api()`` and would answer every valid request with 422
(regression test: tests/test_modal_worker.py).
"""

import base64
import io
import logging
import os
import re
import time

import modal

APP_NAME = "video2slides-image-worker"
MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"
# Pinned commit of the Hugging Face repo, so cached images always match the model that drew them.
MODEL_REVISION = os.environ.get("V2S_MODEL_REVISION", "462165984030d82259a11f4367a4eed129e94a7b")
GPU_TYPE = os.environ.get("V2S_GPU", "L4")
CACHE_DIR = "/cache"

# Limits (keep in sync with app/illustrations/validation.py)
MIN_SIDE, MAX_SIDE, MAX_PIXELS = 512, 1536, 1_100_000
MIN_STEPS, MAX_STEPS = 10, 40
MIN_GUIDANCE, MAX_GUIDANCE = 1.0, 12.0
MAX_PROMPT_CHARS = 900
GENERATION_TIMEOUT_SEC = 180        # per image, including queueing inside the container
IDLE_SCALEDOWN_SEC = 120            # GPU stays warm this long after the last image of a batch
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
CALL_ID_RE = re.compile(r"^fc-[A-Za-z0-9]{10,64}$")

log = logging.getLogger(APP_NAME)

gpu_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "torch==2.5.1",
        "diffusers==0.31.0",
        "transformers==4.46.3",
        "accelerate==1.1.1",
        "safetensors==0.4.5",
        "huggingface_hub==0.26.5",
        "Pillow==11.0.0",
    )
    .env({"HF_HOME": CACHE_DIR, "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false"})
)
web_image = modal.Image.debian_slim(python_version="3.11").pip_install("fastapi[standard]==0.115.6")

app = modal.App(APP_NAME)
model_cache = modal.Volume.from_name("video2slides-hf-cache", create_if_missing=True)
# request_id -> FunctionCall id: makes submits idempotent across retries of the backend.
request_index = modal.Dict.from_name("video2slides-image-requests", create_if_missing=True)


def validate_params(p: dict) -> dict:
    """Validate and normalise generation parameters (raises ValueError)."""
    if not isinstance(p, dict):
        raise ValueError("params must be an object")
    prompt = " ".join(str(p.get("prompt") or "").split())[:MAX_PROMPT_CHARS]
    negative = " ".join(str(p.get("negative_prompt") or "").split())[:MAX_PROMPT_CHARS]
    width, height = int(p.get("width", 1344)), int(p.get("height", 768))
    steps, guidance, seed = int(p.get("steps", 25)), float(p.get("guidance", 6.0)), int(p.get("seed", 0))
    fmt = str(p.get("output_format") or "jpeg").lower()
    if not prompt:
        raise ValueError("empty prompt")
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE) or width % 64 or height % 64:
        raise ValueError("invalid size")
    if width * height > MAX_PIXELS:
        raise ValueError("too many pixels")
    if not (MIN_STEPS <= steps <= MAX_STEPS):
        raise ValueError("invalid steps")
    if not (MIN_GUIDANCE <= guidance <= MAX_GUIDANCE):
        raise ValueError("invalid guidance")
    if not (0 <= seed < 2**31):
        raise ValueError("invalid seed")
    if fmt not in ("jpeg", "png"):
        raise ValueError("invalid output_format")
    return {"prompt": prompt, "negative_prompt": negative, "width": width, "height": height, "steps": steps,
            "guidance": guidance, "seed": seed, "output_format": fmt}


@app.function(image=gpu_image, volumes={CACHE_DIR: model_cache}, timeout=1800, cpu=2.0, memory=4096)
def download_model() -> str:
    """Fill the weights volume on a cheap CPU container (avoids paying GPU time for the download)."""
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        MODEL_ID, revision=MODEL_REVISION, cache_dir=CACHE_DIR,
        allow_patterns=["*.json", "*.txt", "*.fp16.safetensors", "tokenizer*/*"],
    )
    model_cache.commit()
    return path


@app.cls(
    image=gpu_image,
    gpu=GPU_TYPE,
    volumes={CACHE_DIR: model_cache},
    min_containers=0,                   # scale to zero: no GPU billed while idle
    max_containers=1,                   # never more than one GPU worker
    scaledown_window=IDLE_SCALEDOWN_SEC,
    timeout=GENERATION_TIMEOUT_SEC,
    startup_timeout=900,                # first start may still download weights
    retries=0,                          # retries are decided by the backend (bounded, budget-checked)
)
class SDXLWorker:
    @modal.enter()
    def load(self) -> None:
        import torch
        from diffusers import StableDiffusionXLPipeline

        t0 = time.perf_counter()
        self.pipe = StableDiffusionXLPipeline.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, cache_dir=CACHE_DIR,
            torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
        )
        self.pipe.to("cuda")
        self.pipe.set_progress_bar_config(disable=True)
        self.pipe.enable_vae_slicing()   # lower peak memory during decode
        model_cache.commit()             # persist weights if this container had to download them
        self.load_seconds = round(time.perf_counter() - t0, 2)
        self.gpu_name = torch.cuda.get_device_name(0)
        self.first_call = True

    @modal.method()
    def generate(self, params: dict) -> dict:
        import torch

        try:
            p = validate_params(params)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error_code": "invalid_params", "error": str(exc), "retryable": False}

        cold, self.first_call = self.first_call, False
        torch.cuda.reset_peak_memory_stats()
        t0 = time.perf_counter()
        try:
            generator = torch.Generator(device="cuda").manual_seed(p["seed"])
            with torch.inference_mode():
                image = self.pipe(
                    prompt=p["prompt"], negative_prompt=p["negative_prompt"] or None,
                    width=p["width"], height=p["height"], num_inference_steps=p["steps"],
                    guidance_scale=p["guidance"], generator=generator, num_images_per_prompt=1,
                ).images[0]
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            return {"ok": False, "error_code": "cuda_oom", "error": "GPU hết bộ nhớ.", "retryable": True}
        inference = time.perf_counter() - t0

        buf = io.BytesIO()
        if p["output_format"] == "png":
            image.save(buf, "PNG", optimize=True)
        else:
            image.save(buf, "JPEG", quality=92)
        metrics = {
            "inference_seconds": round(inference, 2),
            "peak_vram_mb": round(torch.cuda.max_memory_allocated() / 2**20),
            "gpu": self.gpu_name,
            "cold_start": cold,
            "model_load_seconds": self.load_seconds if cold else 0,
            "steps": p["steps"],
        }
        print(f"generated {p['width']}x{p['height']} steps={p['steps']} in {inference:.1f}s cold={cold}")
        return {"ok": True, "image": buf.getvalue(), "format": p["output_format"], "width": image.width,
                "height": image.height, "seed": p["seed"], "model": f"{MODEL_ID}@{MODEL_REVISION[:12]}",
                "metrics": metrics}


@app.function(image=web_image, min_containers=0, max_containers=2, scaledown_window=60, timeout=60)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(requires_proxy_auth=True)
def api():
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel

    web = FastAPI(title="Video2Slides image worker", docs_url=None, redoc_url=None, openapi_url=None)

    class GenerateIn(BaseModel):
        request_id: str
        params: dict

    @web.get("/v1/health")
    def health() -> dict:  # does not start a GPU
        return {"ok": True, "model": MODEL_ID, "revision": MODEL_REVISION[:12], "gpu": GPU_TYPE}

    @web.post("/v1/generate")
    def generate(body: GenerateIn) -> dict:
        if not REQUEST_ID_RE.match(body.request_id):
            raise HTTPException(status_code=422, detail="invalid request_id")
        try:
            params = validate_params(body.params)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        existing = request_index.get(body.request_id)
        if existing:
            return {"call_id": existing, "deduplicated": True}
        call = SDXLWorker().generate.spawn(params)
        if not request_index.put(body.request_id, call.object_id, skip_if_exists=True):
            call.cancel()  # lost a race with an identical concurrent submit
            return {"call_id": request_index.get(body.request_id), "deduplicated": True}
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
            return {"status": "failed", "error_code": "timeout", "error": "Quá thời gian tạo ảnh.", "retryable": True}
        except modal.exception.InputCancellation:
            return {"status": "failed", "error_code": "cancelled", "error": "Đã hủy.", "retryable": False}
        except modal.exception.NotFoundError:
            raise HTTPException(status_code=404, detail="unknown call") from None
        except Exception as exc:  # container crash etc.; details stay in Modal logs
            return {"status": "failed", "error_code": "worker_error",
                    "error": f"GPU worker lỗi ({exc.__class__.__name__}).", "retryable": True}
        if not out.get("ok"):
            return {"status": "failed", "error_code": out.get("error_code", "worker_error"),
                    "error": out.get("error", ""), "retryable": bool(out.get("retryable"))}
        return {
            "status": "done", "image_b64": base64.b64encode(out["image"]).decode("ascii"), "format": out["format"],
            "width": out["width"], "height": out["height"], "seed": out["seed"], "model": out["model"],
            "metrics": out["metrics"],
        }

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
def smoke_test(prompt: str = "a calm minimalist illustration of a lighthouse at dawn, soft light, no text"):
    """Generate one image directly (bypasses the web API): `modal run infrastructure/modal_image_worker.py`."""
    t0 = time.perf_counter()
    out = SDXLWorker().generate.remote({"prompt": prompt, "negative_prompt": "text, watermark, logo",
                                        "width": 1344, "height": 768, "steps": 25, "guidance": 6.0, "seed": 42})
    total = time.perf_counter() - t0
    if not out.get("ok"):
        raise SystemExit(f"FAILED: {out}")
    with open("smoke_test.jpg", "wb") as fh:
        fh.write(out["image"])
    print(f"saved smoke_test.jpg; wall time {total:.1f}s; metrics {out['metrics']}")
