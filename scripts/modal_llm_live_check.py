"""LIVE check of the deployed Ollama-on-Modal worker (billable: starts one L4 GPU container).

    .venv\\Scripts\\python.exe scripts\\modal_llm_live_check.py            # health, auth, cold + warm request
    .venv\\Scripts\\python.exe scripts\\modal_llm_live_check.py --deck     # + a full staged Vietnamese deck
    .venv\\Scripts\\python.exe scripts\\modal_llm_live_check.py --concurrency  # + 2 parallel requests

Reads MODAL_LLM_WORKER_URL / MODAL_PROXY_TOKEN_* from .env. Prints measured times, GPU evidence
(size_vram / size from Ollama /api/ps, nvidia-smi) and a list-price estimate (not the billed amount).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.ai.modal_ollama import ModalOllamaClient, ModalOllamaProvider  # noqa: E402

SCHEMA = {"type": "object", "properties": {"tieu_de": {"type": "string"},
                                           "y_chinh": {"type": "array", "items": {"type": "string"}}},
          "required": ["tieu_de", "y_chinh"]}
PROMPT = [{"role": "system", "content": "Bạn tóm tắt nội dung bằng tiếng Việt tự nhiên. Chỉ trả JSON."},
          {"role": "user", "content": "<transcript>\n[0] Nước sôi ở 100 độ C ở áp suất tiêu chuẩn.\n"
                                      "[6] Trên núi cao áp suất thấp hơn nên nước sôi ở nhiệt độ thấp hơn.\n"
                                      "</transcript>\nViết tiêu đề và 2-3 ý chính."}]


def chat(client: ModalOllamaClient, label: str) -> dict:
    t0 = time.monotonic()
    res = client.chat(settings.modal_llm_model, PROMPT, format=SCHEMA, num_predict=300, think=False)
    wall = time.monotonic() - t0
    data = json.loads(res.content)
    m = client.calls[-1]
    print(f"[{label}] {wall:.1f}s round trip | gpu={m.get('gpu')} gpu_fraction={m.get('gpu_fraction')} "
          f"vram_model={m.get('size_vram_mb')}MB cold={m.get('cold_start')} startup={m.get('startup_seconds')}s "
          f"load={m.get('model_load_seconds')}s | tokens out={res.output_tokens} "
          f"({res.output_tokens / res.eval_sec if res.eval_sec else 0:.0f} tok/s)")
    print("   ", json.dumps(data, ensure_ascii=False))
    assert m.get("gpu_fraction", 0) >= 0.95, "model is not fully on the GPU"
    return {"wall": wall, **m}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", action="store_true")
    ap.add_argument("--concurrency", action="store_true")
    args = ap.parse_args()

    client = ModalOllamaClient()
    t0 = time.monotonic()
    h = client.health(refresh=True)
    print(f"health ({time.monotonic() - t0:.1f}s, CPU only): model={h.get('model')} ready={h.get('model_ready')} "
          f"ollama={h.get('ollama_version')} limits={h.get('limits')}")
    r = httpx.get(f"{settings.modal_llm_worker_url}/v1/health", timeout=30)
    print(f"unauthenticated request -> HTTP {r.status_code} (expected 401)")
    assert r.status_code == 401
    if not h.get("model_ready"):
        sys.exit("model not ready: run  modal run infrastructure/modal_llm_worker.py::setup_model")

    cold = chat(client, "cold")
    warm = chat(client, "warm")
    print(f"cold vs warm round trip: {cold['wall']:.1f}s vs {warm['wall']:.1f}s")

    if args.concurrency:
        out = {}

        def run(i):
            c = ModalOllamaClient()
            t = time.monotonic()
            c.chat(settings.modal_llm_model, PROMPT, format=SCHEMA, num_predict=200, think=False)
            out[i] = time.monotonic() - t

        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        print(f"2 parallel requests (max 1 container, 1 input each -> queued): {out}")

    if args.deck:
        from app.ai.base import OutlineRequest
        from app.transcribe import Segment, Transcript

        text = (ROOT / "scripts" / "fixtures" / "lecture_vi.txt").read_text(encoding="utf-8")
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        segs = [Segment(i * 6.0, i * 6.0 + 5.5, s) for i, s in enumerate(sents)]
        t = Transcript(segs, "vi", len(segs) * 6.0)
        p = ModalOllamaProvider()
        t0 = time.monotonic()
        pres = p.generate(OutlineRequest(transcript=t, language="vi", slide_count=6,
                                         progress=lambda s, d, n: print(f"   {s} {d}/{n}")))
        info = p.describe()
        print(f"deck: {len(pres.slides)} slides in {time.monotonic() - t0:.1f}s; requests={info['requests']} "
              f"repairs={info['repairs']} modal={info['modal']}")
        for s in pres.slides:
            print(f"   {s.slide_number}. [{s.layout_type}] {s.title} | notes: {s.speaker_notes[:80]}")
    print("usage:", json.dumps(client.usage(), ensure_ascii=False))
    print(f"The {settings.modal_llm_gpu} GPU container stays up for the scale-down window (60 s by default) after "
          "the last request, then scales to zero. Check:  modal container list")


if __name__ == "__main__":
    main()
