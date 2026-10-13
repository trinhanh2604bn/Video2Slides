"""Test configuration: isolated data dir, offline AI, tiny generated media."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="v2s-tests-"))
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["AI_PROVIDER"] = "offline"
os.environ["LLM_PROVIDER"] = "offline"
os.environ["GEMINI_API_KEY"] = ""
os.environ["APP_PASSWORD"] = ""
# Never reach the real Modal workers from tests (cloud tests use tests/fake_modal.py explicitly).
os.environ["MEDIA_PROCESSING"] = "local"
os.environ["MODAL_MEDIA_WORKER_URL"] = ""
os.environ["MODAL_LLM_WORKER_URL"] = ""

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def make_video(path: Path, seconds: float = 6, audio: bool = True, fmt: str = "mp4") -> Path:
    """Generate a synthetic video with changing colour scenes and a tone."""
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=15:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args += ["-f", fmt, str(path)]
    subprocess.run(args, check=True)
    return path


@pytest.fixture(scope="session")
def sample_mp4(tmp_path_factory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    return make_video(tmp_path_factory.mktemp("media") / "sample.mp4")
