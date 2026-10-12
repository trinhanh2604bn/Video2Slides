"""Pre-download the configured Whisper model into MODEL_CACHE_DIR.

Run once after installing dependencies so the first user request doesn't wait
for a ~75-500 MB download:

    python scripts/download_model.py            # uses WHISPER_MODEL (default: base)
    python scripts/download_model.py tiny small
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ALLOWED_WHISPER_MODELS, settings  # noqa: E402


def main(names: list[str]) -> int:
    from faster_whisper.utils import download_model

    settings.model_cache_dir.mkdir(parents=True, exist_ok=True)
    for name in names or [settings.whisper_model]:
        if name not in ALLOWED_WHISPER_MODELS:
            print(f"skip {name}: allowed models are {sorted(ALLOWED_WHISPER_MODELS)}")
            continue
        start = time.time()
        path = download_model(name, cache_dir=str(settings.model_cache_dir))
        print(f"{name}: ready at {path} ({time.time() - start:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
