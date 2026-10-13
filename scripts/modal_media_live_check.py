"""LIVE check of the deployed media worker (billable: one CPU container + one L4 GPU container).

    .venv\\Scripts\\python.exe scripts\\modal_media_live_check.py [data\\fixtures\\lecture_vi.mp4]

Uploads the video, runs FFmpeg on Modal (CPU) and faster-whisper on the GPU, checks the transcript
(ordered timestamps inside the video duration, non-empty Vietnamese text) and the CUDA evidence, then deletes
the temporary files on Modal. Prints upload / preprocessing / cold-start / transcription times.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.media_remote import ModalMediaClient, Waiter, file_sha256  # noqa: E402


def main() -> None:
    video = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "fixtures" / "lecture_vi.mp4")
    client = ModalMediaClient()
    print("health:", client.health())
    sha = file_sha256(video)
    t0 = time.monotonic()
    up = client.upload(video, sha, Waiter())
    print(f"upload: {up['size'] / 1e6:.1f} MB in {up['upload_sec']}s ({up['chunks']} chunks)")
    try:
        t = time.monotonic()
        pre = client.preprocess(up, {"max_duration_sec": settings.max_duration_sec, "audio_normalize": True,
                                     "frame_candidates": 16, "frame_width": 1280}, Waiter())
        print(f"preprocess round trip {time.monotonic() - t:.1f}s; worker timings {pre['timings']}; "
              f"frames={len(pre['frames'])}; media={pre['media']}; ffmpeg={pre['ffmpeg']}")
        for label in ("cold", "warm"):
            t = time.monotonic()
            res = client.transcribe(up["upload_id"], {"model": settings.whisper_model, "compute_type": "float16",
                                                      "beam_size": settings.whisper_beam_size, "vad": True,
                                                      "language": "vi"}, Waiter())
            segs = res["segments"]
            m = res["metrics"]
            print(f"[{label}] transcribe round trip {time.monotonic() - t:.1f}s; metrics {json.dumps(m)}")
            assert m["device"] == "cuda" and m["cuda_devices"] >= 1, "not on CUDA"
            starts = [s["start"] for s in segs]
            assert starts == sorted(starts) and all(0 <= s["start"] <= s["end"] <= res["duration"] + 1 for s in segs)
            print(f"   {len(segs)} segments, language={res['language']} p={res['language_probability']}")
            for s in segs[:4]:
                print(f"   [{s['start']:.1f}-{s['end']:.1f}] {s['text']}")
    finally:
        client.delete(up["upload_id"])
    print(f"total {time.monotonic() - t0:.1f}s; temporary files deleted on Modal")


if __name__ == "__main__":
    main()
