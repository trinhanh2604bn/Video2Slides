"""Live check of the local LLM (real Ollama, no mocks).

    python scripts/ollama_live_check.py                      # status + one small structured request
    python scripts/ollama_live_check.py data/jobs/<id>/transcript.json --slides 6
                                                             # full deck generation from a saved transcript

Prints readiness, timings (cold start = model load time of the first request), tokens/second,
retries and the generated slides with their notes, so quality can be reviewed by a person.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai.base import AIError, OutlineRequest  # noqa: E402
from app.ai.health import llm_status  # noqa: E402
from app.ai.ollama import OllamaProvider  # noqa: E402
from app.ai.ollama_client import OllamaClient  # noqa: E402
from app.config import settings  # noqa: E402
from app.transcribe import Transcript  # noqa: E402


def small_request() -> None:
    schema = {"type": "object", "properties": {"title": {"type": "string"},
                                               "points": {"type": "array", "items": {"type": "string"}}},
              "required": ["title", "points"]}
    msgs = [{"role": "system", "content": "Trả lời bằng tiếng Việt, chỉ JSON."},
            {"role": "user", "content": "Đặt tiêu đề và 3 ý chính cho bài giảng về quản lý thời gian."}]
    client = OllamaClient()
    for label in ("lần 1", "lần 2 (model đã nạp)"):
        t0 = time.monotonic()
        res = client.chat(settings.ollama_model, msgs, format=schema, num_predict=300,
                          think=False if "thinking" in client.show(settings.ollama_model).capabilities else None)
        print(f"  {label}: {time.monotonic() - t0:.1f} s, nạp model {res.load_sec:.1f} s, "
              f"{res.output_tokens} token, {res.stats()['tokens_per_sec']} token/s, done_reason={res.done_reason}")
        print("   ", json.loads(res.content))


def full_deck(path: Path, slides: int | None, images: int) -> int:
    t = Transcript.from_json(json.loads(path.read_text(encoding="utf-8")))
    out = path.parent
    p = OllamaProvider()
    t0 = time.monotonic()
    req = OutlineRequest(transcript=t, language=t.language or "vi", slide_count=slides, max_illustrations=images,
                         cache_dir=out,
                         progress=lambda s, d, n: print(f"  {time.monotonic() - t0:6.0f}s  {s} {d}/{n}", flush=True))
    try:
        pres = p.generate(req)
    except AIError as exc:
        print(f"LỖI [{exc.code}]: {exc}")
        return 1
    finally:
        print(json.dumps({k: v for k, v in p.describe().items() if k != "calls"}, ensure_ascii=False, indent=1))
    for s in pres.slides:
        print(f"\n#{s.slide_number} {s.slide_id} [{s.layout_type}] {s.title}")
        for k in s.key_points:
            print("   -", k)
        for i in s.items:
            print("   *", i.heading, ":", i.text)
        print("   Speaker notes:", s.speaker_notes)
        print("   Tóm tắt:", s.reading_notes.summary)
        print("   Giải thích:", s.reading_notes.detailed_explanation.replace("\n", " / "))
        print("   Ví dụ:", s.reading_notes.examples, "| Cần nhớ:", s.reading_notes.key_takeaway)
        print("   Nguồn:", [(r.start, r.end) for r in s.source_ranges], "| Ảnh:", s.image_prompt)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("transcript", nargs="?", type=Path)
    ap.add_argument("--slides", type=int, default=0, help="0 = tự động")
    ap.add_argument("--images", type=int, default=0, help="số slide được viết prompt ảnh")
    args = ap.parse_args()
    st = llm_status(provider="ollama")
    print(st.get("message"))
    if not st.get("ready"):
        return 1
    print(f"  Model: {st['model']} ({st.get('parameter_size')}, {st.get('quantization')}), "
          f"khả năng: {', '.join(st.get('capabilities') or [])}; RAM trống: {(st.get('memory') or {}).get('available_mb')} MB")
    if args.transcript:
        return full_deck(args.transcript, args.slides or None, args.images)
    small_request()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
