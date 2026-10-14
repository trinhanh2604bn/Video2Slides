"""In-process fakes of the private Modal workers' HTTPS APIs (httpx MockTransport). MOCK: no GPU, no model.

* ``FakeModalLLM``   - /v1/health, /v1/chat, /v1/result, /v1/cancel of infrastructure/modal_llm_worker.py; chat
                       answers come from tests/fake_ollama.py so the staged generation is exercised end to end.
* ``FakeModalMedia`` - upload chunks, preprocess (frames + metadata), transcribe (segments + CUDA metrics),
                       progress, cancel and delete of infrastructure/modal_media_worker.py.
* ``use_settings``   - replace the frozen settings object everywhere it is imported.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import re

import httpx

from tests.fake_ollama import FakeOllama

TOKEN = {"Modal-Key": "wk-test", "Modal-Secret": "ws-test"}


def use_settings(monkeypatch, **changes):
    """Frozen settings are imported by name in several modules: patch every reference."""
    import app.ai as ai_pkg
    from app import config, main
    from app.ai import gemini, health, ollama, ollama_client, router

    new = dataclasses.replace(config.settings, **changes)
    for mod in (config, main, ai_pkg, gemini, health, ollama, ollama_client, router):
        monkeypatch.setattr(mod, "settings", new, raising=False)
    return new


def _authorised(request: httpx.Request) -> bool:
    return request.headers.get("Modal-Key") == TOKEN["Modal-Key"] and \
        request.headers.get("Modal-Secret") == TOKEN["Modal-Secret"]


class FakeModalLLM:
    def __init__(self, model_ready: bool = True, pending_polls: int = 1):
        self.ollama = FakeOllama()
        self.model_ready = model_ready
        self.pending_polls = pending_polls
        self.health_calls = 0
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.calls: dict[str, dict] = {}
        self.fail: dict | None = None      # {"error_code", "error", "retryable"} for every chat
        self.gpu_fraction = 1.0
        self.max_num_ctx: int | None = None  # None = an older worker that does not advertise limits

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not _authorised(request):
            return httpx.Response(401, text="modal-http: missing credentials for proxy authorization")
        path = request.url.path
        if path == "/v1/health":
            self.health_calls += 1
            return httpx.Response(200, json={
                "ok": True, "model": "qwen3:4b", "ollama_version": "0.40.3", "gpu": "L4",
                "model_ready": self.model_ready,
                **({"limits": {"max_num_ctx": self.max_num_ctx}} if self.max_num_ctx else {}),
                "model_info": {"capabilities": ["completion", "thinking"], "parameter_size": "4.0B",
                               "quantization": "Q4_K_M", "family": "qwen3", "context_length": 262144}
                if self.model_ready else {}})
        if path == "/v1/chat":
            body = json.loads(request.content)
            req = body["request"]
            self.submitted.append(req)
            call_id = f"fc-{len(self.submitted):012d}"
            if self.fail:
                self.calls[call_id] = {"status": "failed", **self.fail}
            else:
                self.calls[call_id] = {"status": "done", "result": self._answer(req, first=len(self.submitted) == 1)}
            self.calls[call_id]["polls"] = 0
            return httpx.Response(200, json={"call_id": call_id, "deduplicated": False})
        if path.startswith("/v1/result/"):
            call = self.calls.get(path.rsplit("/", 1)[1])
            if call is None:
                return httpx.Response(404, json={"detail": "unknown call"})
            call["polls"] += 1
            if call["polls"] <= self.pending_polls:
                return httpx.Response(200, json={"status": "pending"})
            return httpx.Response(200, json={k: v for k, v in call.items() if k != "polls"})
        if path.startswith("/v1/cancel/"):
            self.cancelled.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"cancelled": True})
        return httpx.Response(404, json={"detail": "not found"})

    def _answer(self, req: dict, first: bool) -> dict:
        body = {**req, "stream": True}
        resp = self.ollama.handle(httpx.Request("POST", "http://ollama.local/api/chat", json=body))
        if resp.status_code != 200:
            raise AssertionError(resp.text)
        content, final = [], {}
        for line in resp.content.decode().splitlines():
            msg = json.loads(line)
            content.append((msg.get("message") or {}).get("content") or "")
            if msg.get("done"):
                final = msg
        metrics = {"cold_start": first, "wall_seconds": 3.0, "size_mb": 3700, "size_vram_mb": round(3700 * self.gpu_fraction),
                   "gpu_fraction": self.gpu_fraction, "gpu": "NVIDIA L4", "vram_total_mb": 23034, "vram_used_mb": 5200}
        if first:
            metrics.update(startup_seconds=20.0, model_load_seconds=6.0)
        return {"ok": True, "content": "".join(content), "thinking": "", "done_reason": final.get("done_reason", "stop"),
                "prompt_eval_count": final.get("prompt_eval_count", 0), "eval_count": final.get("eval_count", 0),
                "load_sec": 0.1, "total_sec": 3.0, "eval_sec": 2.0, "prompt_eval_sec": 0.5,
                "model": "qwen3:4b", "metrics": metrics}

    def provider(self):
        from app.ai.modal_ollama import ModalOllamaClient, ModalOllamaProvider

        client = ModalOllamaClient(base_url="https://llm.modal.test", token_id=TOKEN["Modal-Key"],
                                   token_secret=TOKEN["Modal-Secret"], client=httpx.Client(transport=self.transport()))
        return ModalOllamaProvider(client=client)


def jpeg(color=(30, 120, 200), size=(320, 180)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    for i in range(0, size[0], 20):  # some structure so frames are not "flat"
        draw.line([(i, 0), (size[0] - i, size[1])], fill=(255 - color[0], 200, color[2] // 2), width=3)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class FakeModalMedia:
    def __init__(self, duration: float = 6.0, device: str = "cuda"):
        self.duration = duration
        self.device = device
        self.uploads: dict[str, dict] = {}
        self.calls: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.deleted: list[str] = []
        self.transcribe_calls = 0
        self.preprocess_calls = 0
        self.preprocess_params: list[dict] = []
        self.transcribe_params: list[dict] = []
        self.pending_polls = 1
        self.transcribe_error: dict | None = None
        self.hold_transcription = False  # stays pending until cancelled
        self.segments = [{"start": float(i), "end": i + 0.9, "text": f"Câu số {i} nói về kỹ năng làm việc nhóm."}
                         for i in range(5)]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _call(self, payload: dict) -> httpx.Response:
        call_id = f"fc-{len(self.calls) + 1:012d}"
        self.calls[call_id] = {**payload, "polls": 0}
        return httpx.Response(200, json={"call_id": call_id})

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not _authorised(request):
            return httpx.Response(401, text="modal-http: missing credentials for proxy authorization")
        path, method = request.url.path, request.method
        if path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "gpu": "L4"})
        if path == "/v1/uploads" and method == "POST":
            uid = f"{len(self.uploads) + 1:032x}"
            self.uploads[uid] = {"chunks": {}}
            return httpx.Response(200, json={"upload_id": uid, "max_chunk_bytes": 32 << 20})
        m = re.match(r"^/v1/uploads/([0-9a-f]{32})/chunks/(\d+)$", path)
        if m and method == "PUT":
            self.uploads[m.group(1)]["chunks"][int(m.group(2))] = request.content
            return httpx.Response(200, json={"index": int(m.group(2)), "bytes": len(request.content)})
        m = re.match(r"^/v1/uploads/([0-9a-f]{32})/complete$", path)
        if m:
            self.preprocess_calls += 1
            body = json.loads(request.content)
            self.preprocess_params.append(body["params"])
            up = self.uploads[m.group(1)]
            data = b"".join(up["chunks"][i] for i in sorted(up["chunks"]))
            if hashlib.sha256(data).hexdigest() != body["sha256"] or len(data) != body["size"]:
                return self._call({"status": "failed", "error_code": "upload_corrupt", "error": "corrupt",
                                   "retryable": False})
            frames = [{"timestamp": 1.0 + i, "jpeg_b64": base64.b64encode(jpeg((40 + 60 * i, 90, 160))).decode()}
                      for i in range(3)]
            return self._call({"status": "done", "result": {
                "ok": True, "frames": frames, "audio": {"bytes": 192000},
                "media": {"duration": self.duration, "width": 640, "height": 360, "video_codec": "h264",
                          "audio_codec": "aac", "format_name": "mov,mp4", "size_bytes": len(data)},
                "timings": {"assemble_sec": 0.1, "probe_sec": 0.1, "audio_sec": 0.5, "frames_sec": 0.4},
                "ffmpeg": {"location": "modal-cpu", "gpu_operations": []}}})
        if path == "/v1/transcribe":
            self.transcribe_calls += 1
            self.transcribe_params.append(json.loads(request.content)["params"])
            if self.transcribe_error:
                return self._call({"status": "failed", **self.transcribe_error})
            return self._call({"status": "done", "hold": self.hold_transcription, "result": {
                "ok": True, "segments": self.segments, "language": "vi", "language_probability": 0.99,
                "duration": self.duration, "metrics": {"device": self.device, "compute_type": "float16",
                                                       "model": "base", "gpu": "NVIDIA L4", "cuda_devices": 1,
                                                       "model_vram_mb": 300, "transcribe_sec": 1.2}}})
        if path.startswith("/v1/progress/"):
            return httpx.Response(200, json={"done_sec": 3.0, "total_sec": self.duration})
        if path.startswith("/v1/result/"):
            call = self.calls[path.rsplit("/", 1)[1]]
            call["polls"] += 1
            if call.get("cancelled"):
                return httpx.Response(200, json={"status": "failed", "error_code": "cancelled", "error": "Đã hủy.",
                                                 "retryable": False})
            if call["polls"] <= self.pending_polls or call.get("hold"):
                return httpx.Response(200, json={"status": "pending"})
            return httpx.Response(200, json={k: v for k, v in call.items() if k not in ("polls", "hold")})
        if path.startswith("/v1/cancel/"):
            cid = path.rsplit("/", 1)[1]
            self.cancelled.append(cid)
            self.calls[cid]["cancelled"] = True
            return httpx.Response(200, json={"cancelled": True})
        m = re.match(r"^/v1/uploads/([0-9a-f]{32})$", path)
        if m and method == "DELETE":
            self.deleted.append(m.group(1))
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"detail": "not found"})

    def client_factory(self):
        from app.media_remote import ModalMediaClient

        def make():
            return ModalMediaClient(base_url="https://media.modal.test", token_id=TOKEN["Modal-Key"],
                                    token_secret=TOKEN["Modal-Secret"], client=httpx.Client(transport=self.transport()),
                                    sleep=lambda s: None)
        return make
