"""In-process fake of Ollama's native API (httpx MockTransport). MOCK: no model runs here.

It answers /api/chat with deterministic structured JSON chosen from the request's ``format`` schema
(chunk notes, outline or a batch of slides), streamed as NDJSON like the real server. Tests can
override any step through ``FakeOllama.overrides`` to inject invalid, truncated or failing answers.
"""

from __future__ import annotations

import json
import re
from typing import Callable

import httpx


def _step(schema: dict) -> str:
    props = schema.get("properties", {})
    if "facts" in props:
        return "chunk"
    if "presentation_title" in props:
        return "outline"
    return "slides"


class FakeOllama:
    def __init__(self, models=("qwen3:4b",), capabilities=("completion", "thinking")):
        self.models = list(models)
        self.capabilities = list(capabilities)
        self.chats: list[dict] = []
        # step -> callable(request_body, call_index) -> (content, done_reason) | httpx.Response
        self.overrides: dict[str, Callable] = {}
        self.thinking = ""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ routing

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.3"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": self.models[0], "size": 3_700 * 2**20,
                                                         "size_vram": 0, "context_length": 8192}]})
        if path == "/api/show":
            name = json.loads(request.content)["model"]
            if name not in self.models:
                return httpx.Response(404, json={"error": f"model '{name}' not found"})
            return httpx.Response(200, json={"capabilities": self.capabilities,
                                             "details": {"parameter_size": "4.0B", "quantization_level": "Q4_K_M",
                                                         "family": "qwen3"},
                                             "model_info": {"qwen3.context_length": 262144}})
        if path == "/api/chat":
            body = json.loads(request.content)
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": f"model \"{body['model']}\" not found, try pulling it first"})
            self.chats.append(body)
            step = _step(body["format"])
            idx = sum(1 for c in self.chats if _step(c["format"]) == step) - 1
            if step in self.overrides:
                out = self.overrides[step](body, idx)
                if isinstance(out, httpx.Response):
                    return out
                content, reason = out
            else:
                content, reason = json.dumps(getattr(self, f"answer_{step}")(body), ensure_ascii=False), "stop"
            return self.stream(content, reason)
        return httpx.Response(404, json={"error": "not found"})

    def stream(self, content: str, done_reason: str = "stop") -> httpx.Response:
        lines = []
        if self.thinking:
            lines.append({"message": {"role": "assistant", "content": "", "thinking": self.thinking}, "done": False})
        for i in range(0, len(content), 40):
            lines.append({"message": {"role": "assistant", "content": content[i:i + 40]}, "done": False})
        lines.append({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": done_reason,
                      "prompt_eval_count": 900, "eval_count": max(1, len(content) // 3),
                      "load_duration": 2_000_000_000, "prompt_eval_duration": 1_000_000_000,
                      "eval_duration": 3_000_000_000, "total_duration": 6_000_000_000})
        return httpx.Response(200, content="\n".join(json.dumps(x, ensure_ascii=False) for x in lines).encode())

    # ------------------------------------------------------------------ default answers

    @staticmethod
    def times(body: dict) -> list[float]:
        return [float(x) for x in re.findall(r"^\[(\d+(?:\.\d+)?)", body["messages"][-1]["content"], re.M)]

    def answer_chunk(self, body: dict) -> dict:
        ts = self.times(body) or [0.0]
        return {"summary": "Phần này giải thích một nhóm ý trong bài giảng.",
                "facts": [{"text": f"Ý quan trọng tại giây {t:.0f}.", "start": t, "end": t + 5} for t in ts[:6]],
                "terms": ["quản lý thời gian"]}

    def answer_outline(self, body: dict) -> dict:
        n = body["format"]["properties"]["slides"]["minItems"]
        ts = self.times(body) or [0.0]
        layouts = ["title"] + ["bullets", "process", "cards", "concept", "two_column"] * 6
        slides = []
        for i in range(n):
            layout = layouts[i] if i < n - 1 else "summary"
            slides.append({"title": f"Chủ đề {i + 1}", "layout_type": layout, "purpose": "Giải thích ý chính.",
                           "source_ranges": [] if i == 0 else [{"start": ts[i % len(ts)], "end": ts[i % len(ts)] + 1}]})
        return {"presentation_title": "Quản lý thời gian", "subtitle": "Tóm tắt bài giảng", "slides": slides}

    def slide_for(self, sid: str, body: dict, image: bool = False) -> dict:
        req = body["messages"][-1]["content"]
        m = re.search(rf"slide_id: {sid} \| planned title: (.*?) \| layout: (\w+)", req)
        title, layout = (m.group(1), m.group(2)) if m else ("Tiêu đề", "bullets")
        s = {"slide_id": sid, "title": title, "subtitle": "", "layout_type": layout,
             "key_points": ["Ý chính thứ nhất", "Ý chính thứ hai"], "items": [],
             "left_title": "", "left_points": [], "right_title": "", "right_points": [],
             "speaker_notes": f"Thuyết minh cho {sid}.",
             "reading_notes": {"summary": f"Tóm tắt {sid}.", "detailed_explanation": f"Giải thích chi tiết {sid}.",
                               "examples": [], "key_takeaway": "Điểm cần nhớ."},
             "visual_type": "none", "visual_description": "Sơ đồ đơn giản.",
             "source_ranges": [{"start": 1.0, "end": 2.0}]}
        if layout == "title":
            s["subtitle"] = "Tóm tắt video"
        if layout in ("process", "cards", "concept"):
            s["items"] = [{"heading": "Bước 1", "text": "Làm A"}, {"heading": "Bước 2", "text": "Làm B"},
                          {"heading": "Bước 3", "text": "Làm C"}]
            if layout == "concept":
                s["subtitle"] = "Định nghĩa ngắn."
        if layout == "two_column":
            s.update(left_title="Trái", left_points=["a", "b"], right_title="Phải", right_points=["c", "d"])
        if "image_prompt" in body["format"]["properties"]["slides"]["items"]["properties"]:
            s["image_prompt"] = "a calm desk with an hourglass and a notebook, morning light" if image else ""
        return s

    def answer_slides(self, body: dict) -> dict:
        ids = body["format"]["properties"]["slides"]["items"]["properties"]["slide_id"]["enum"]
        req = body["messages"][-1]["content"]
        out = []
        for sid in ids:
            image = bool(re.search(rf"slide_id: {sid} .*illustration: yes", req))
            out.append(self.slide_for(sid, body, image))
        return {"slides": out}

    def steps(self) -> list[str]:
        return [_step(c["format"]) for c in self.chats]
