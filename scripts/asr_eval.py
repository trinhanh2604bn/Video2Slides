"""LIVE ASR evaluation on the Modal media worker (billable: one L4 GPU container, a few minutes).

    .venv\\Scripts\\python.exe scripts\\asr_eval.py VIDEO.mp4 REFERENCE.txt [config ...]

Uploads the video once, preprocesses it once, then transcribes it with each configuration on the same warm GPU
container and prints the syllable error rate (WER over Vietnamese syllables, case and punctuation ignored),
GPU time and the cold-start breakdown. Configurations are named presets (see PRESETS) or key=value lists such as
"model=small,beam_size=5,batch_size=16". Only models already allowed by the worker can be used.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.media_remote import ModalMediaClient, Waiter, file_sha256  # noqa: E402

GENERIC_PROMPT = "Bài giảng bằng tiếng Việt. Văn bản viết đúng chính tả, có dấu câu."
PRESETS = {
    "base-b1": {"model": "base", "beam_size": 1},
    "small-b1": {"model": "small", "beam_size": 1},
    "small-b5": {"model": "small", "beam_size": 5},
    "small-b5-prompt": {"model": "small", "beam_size": 5, "initial_prompt": GENERIC_PROMPT},
    "small-b5-prev": {"model": "small", "beam_size": 5, "condition_on_previous_text": True},
    "small-batch16-b1": {"model": "small", "beam_size": 1, "batch_size": 16},
    "small-batch16-b5": {"model": "small", "beam_size": 5, "batch_size": 16},
    "small-int8-b5": {"model": "small", "beam_size": 5, "compute_type": "int8_float16"},
}


def syllables(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    return re.findall(r"\w+", text)


def error_rate(ref: list[str], hyp: list[str]) -> float:
    sm = difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal") / len(ref)


def parse(cfg: str) -> tuple[str, dict]:
    if cfg in PRESETS:
        return cfg, PRESETS[cfg]
    out = {}
    for kv in cfg.split(","):
        k, v = kv.split("=", 1)
        out[k] = int(v) if v.isdigit() else (v == "true" if v in ("true", "false") else v)
    return cfg, out


def main() -> None:
    video, ref_file, *cfgs = sys.argv[1:]
    ref = syllables(Path(ref_file).read_text(encoding="utf-8"))
    cfgs = cfgs or list(PRESETS)
    client = ModalMediaClient()
    t0 = time.monotonic()
    up = client.upload(Path(video), file_sha256(Path(video)), Waiter())
    print(f"upload {up['size'] / 1e6:.1f} MB: {up['upload_sec']}s")
    try:
        pre = client.preprocess(up, {"max_duration_sec": 6000, "audio_normalize": True, "frame_candidates": 4}, Waiter())
        print(f"preprocess {pre['timings']}")
        rows = []
        for cfg in cfgs:
            name, params = parse(cfg)
            params = {"compute_type": "float16", "vad": True, "language": "vi", **params}
            sent = time.time()
            t = time.monotonic()
            res = client.transcribe(up["upload_id"], params, Waiter())
            rt = time.monotonic() - t
            m = res["metrics"]
            hyp = " ".join(s["text"] for s in res["segments"])
            er = error_rate(ref, syllables(hyp))
            if m.get("cold_start"):
                print(f"cold start: submit->container start {m['container_started_at'] - sent:.1f}s, "
                      f"enter {m['enter_sec']}s, container start->call {m['call_received_at'] - m['container_started_at']:.1f}s, "
                      f"model load {m['model_load_sec']}s")
            segs = res["segments"]
            ordered = all(a["start"] <= b["start"] for a, b in zip(segs, segs[1:]))
            inside = all(0 <= x["start"] <= x["end"] <= res["duration"] + 1 for x in segs)
            longest = max((x["end"] - x["start"] for x in segs), default=0)
            print(f"   timestamps ordered={ordered} inside_video={inside} longest_segment={longest:.1f}s")
            rows.append((name, er, m["transcribe_sec"], m["model_load_sec"], rt, m["realtime_factor"],
                         m.get("model_vram_mb"), len(res["segments"])))
            print(f"{name:20} SER {er:6.1%} | transcribe {m['transcribe_sec']:6.2f}s x{m['realtime_factor']} realtime"
                  f" | load {m['model_load_sec']}s | round trip {rt:.1f}s | segments {len(res['segments'])}", flush=True)
            Path(ROOT / "data" / "asr_eval").mkdir(parents=True, exist_ok=True)
            (ROOT / "data" / "asr_eval" / f"{Path(video).stem}.{name}.json").write_text(
                json.dumps({"params": params, "ser": er, "metrics": m, "text": hyp, "segments": segs}, ensure_ascii=False, indent=1),
                encoding="utf-8")
    finally:
        client.delete(up["upload_id"])
    print(f"total {time.monotonic() - t0:.1f}s")


if __name__ == "__main__":
    main()
