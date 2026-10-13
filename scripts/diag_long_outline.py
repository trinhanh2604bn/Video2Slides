"""Diagnose the staged LLM route on a finished/failed job's checkpoints (LIVE: uses the Modal LLM worker).

    .venv\\Scripts\\python.exe scripts\\diag_long_outline.py JOB_ID [--slides N]

Copies the job's transcript and LLM checkpoints to data/diag/JOB_ID (the job itself is not modified), runs the
Modal-only staged provider on them and prints every request: step, budget, time, tokens, finish reason.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.ai.base import OutlineRequest  # noqa: E402
from app.ai.modal_ollama import ModalOllamaClient, ModalOllamaProvider  # noqa: E402
from app.ai.staged import StagedRoutedProvider  # noqa: E402
from app.transcribe import Transcript  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--slides", type=int, default=0)
    a = ap.parse_args()
    src = ROOT / "data" / "jobs" / a.job
    work = ROOT / "data" / "diag" / a.job
    if not work.exists():
        work.mkdir(parents=True)
        if (src / "llm_cache").exists():
            shutil.copytree(src / "llm_cache", work / "llm_cache")
    t = Transcript.from_json(json.loads((src / "transcript.json").read_text(encoding="utf-8")))

    real_chat = ModalOllamaClient.chat

    def timed(self, model, messages, **kw):
        t0 = time.monotonic()
        step = "slides" if "slide_id" in json.dumps(kw.get("format") or {}) else (
            "outline" if "presentation_title" in json.dumps(kw.get("format") or {}) else "chunk")
        print(f"-> {step} num_predict={kw.get('num_predict')} prompt_chars={sum(len(m['content']) for m in messages)}",
              flush=True)
        dump = work / "requests"
        dump.mkdir(exist_ok=True)
        (dump / f"{len(list(dump.iterdir())):03d}-{step}.json").write_text(json.dumps(
            {"model": model, "messages": messages, "format": kw.get("format"), "num_ctx": kw.get("num_ctx"),
             "num_predict": kw.get("num_predict"), "think": kw.get("think")}, ensure_ascii=False), encoding="utf-8")
        try:
            res = real_chat(self, model, messages, **kw)
        except Exception as exc:
            print(f"   FAILED after {time.monotonic() - t0:.0f}s: {exc}", flush=True)
            raise
        print(f"   ok {time.monotonic() - t0:.0f}s out={res.output_tokens} done={res.done_reason}", flush=True)
        return res

    ModalOllamaClient.chat = timed
    p = StagedRoutedProvider(None, ModalOllamaProvider)
    t0 = time.monotonic()
    pres = p.generate(OutlineRequest(transcript=t, language=t.language or "en", slide_count=a.slides or None,
                                     cache_dir=work, progress=lambda s, d, n: None))
    info = p.describe()
    print(f"done: {len(pres.slides)} slides in {time.monotonic() - t0:.0f}s; requests={info['requests']} "
          f"cache_hits={info['cache_hits']} repairs={info['repairs']} warnings={info['warnings']}")


if __name__ == "__main__":
    main()
