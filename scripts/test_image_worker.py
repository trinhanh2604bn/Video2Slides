"""Admin smoke test for the deployed GPU worker, run from the backend (e.g. Replit Shell).

    python scripts/test_image_worker.py --health        # no GPU used
    python scripts/test_image_worker.py --count 1       # one image: cold start + inference time
    python scripts/test_image_worker.py --count 3       # small batch on one warm container

Uses the same provider, limits and usage ledger as the app (AI_IMAGES_ENABLED must be true),
so every image here counts towards the daily/monthly limits. Images go to data/worker_test/.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.illustrations.base import ImageGenerationError, ImageRequest  # noqa: E402
from app.illustrations.service import get_image_provider  # noqa: E402
from app.illustrations.styles import BASE_NEGATIVE, get_visual_style  # noqa: E402
from app.illustrations.usage import estimate_cost_usd, reserve  # noqa: E402
from app.illustrations.validation import sanitize_image  # noqa: E402

SUBJECTS = [
    "a lighthouse on a calm coast at sunrise guiding small boats",
    "a green seedling growing from an open book on a wooden desk",
    "a team of paper airplanes flying together in formation through a bright sky",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1, choices=(1, 2, 3))
    ap.add_argument("--style", default="professional", choices=("professional", "academic", "creative"))
    ap.add_argument("--health", action="store_true", help="only check that the endpoint answers (no GPU)")
    args = ap.parse_args()
    s = config.settings

    provider = get_image_provider()
    print(f"provider={provider.name} model={provider.model_id}")
    if args.health:
        print(provider.health())
        return 0
    if not s.ai_images_enabled:
        print("AI_IMAGES_ENABLED is not true - refusing to use the GPU.")
        return 2

    print(f"Estimated cost for {args.count} image(s) incl. cold start + idle window: ${estimate_cost_usd(args.count):.4f}")
    reserve(args.count)
    vs = get_visual_style(args.style)
    out_dir = s.data_dir / "worker_test"
    out_dir.mkdir(parents=True, exist_ok=True)

    t_batch = time.perf_counter()
    refs = []
    for i in range(args.count):
        req = ImageRequest("worker-test", f"slide-{i + 1:02d}", "test", "", f"{SUBJECTS[i]}, {vs.suffix()}",
                           BASE_NEGATIVE, vs.key, "16:9", s.image_width, s.image_height, 1000 + i, s.image_steps,
                           s.image_guidance)
        refs.append((req, provider.submit(req, idempotency_key=f"{req.output_id}-t{int(time.time())}"), time.perf_counter()))
    total_gpu = 0.0
    for req, ref, t0 in refs:
        try:
            res = provider.retrieve_image(ref, timeout=s.image_batch_timeout_sec, poll_interval=2)
        except ImageGenerationError as exc:
            print(f"{req.slide_id}: FAILED ({exc.code}) {exc}")
            return 1
        data, w, h = sanitize_image(res.data, req.width, req.height)
        path = out_dir / f"{req.slide_id}.jpg"
        path.write_bytes(data)
        m = res.metrics
        total_gpu += float(m.get("inference_seconds") or 0)
        print(f"{req.slide_id}: {w}x{h} -> {path}  wall={time.perf_counter() - t0:.1f}s  "
              f"inference={m.get('inference_seconds')}s cold_start={m.get('cold_start')} "
              f"model_load={m.get('model_load_seconds')}s vram={m.get('peak_vram_mb')}MB gpu={m.get('gpu')}")
    wall = time.perf_counter() - t_batch
    print(f"batch wall time {wall:.1f}s, GPU inference {total_gpu:.1f}s")
    print(f"approx. GPU cost of this run (wall time + {120}s idle window at ${s.image_gpu_price_per_sec}/s): "
          f"${(wall + 120) * s.image_gpu_price_per_sec:.4f}  -> verify in Modal dashboard > Usage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
