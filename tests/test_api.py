import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, pipeline
from app.transcribe import Segment, Transcript
from tests.conftest import needs_ffmpeg


@pytest.fixture
def client(monkeypatch):
    def fake_transcribe(audio, language=None, progress=None):
        if progress:
            progress(3.0, 6.0)
        segs = [Segment(i * 1.0, i * 1.0 + 1, f"Câu thử nghiệm số {i} về quản lý thời gian hiệu quả.") for i in range(6)]
        return Transcript(segs, "vi", 6.0)

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    with TestClient(main.app) as c:
        yield c


def _wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_health_and_config(client):
    assert client.get("/api/health").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["max_upload_mb"] == 6144 and cfg["max_duration_sec"] == 7200  # 120-minute videos
    assert set(cfg["styles"]) == {"professional", "academic", "creative"}
    assert cfg["ai_provider"] == "offline"


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Video2Slides" in r.text


def test_rejects_wrong_extension(client):
    r = client.post("/api/jobs", files={"file": ("x.avi", b"123", "video/avi")})
    assert r.status_code == 400


@needs_ffmpeg
def test_rejects_invalid_mp4_and_cleans_up(client):
    before = set(jobs.settings.jobs_dir.iterdir())
    r = client.post("/api/jobs", files={"file": ("x.mp4", b"garbage" * 50, "video/mp4")})
    assert r.status_code == 400
    assert set(jobs.settings.jobs_dir.iterdir()) == before


def test_rejects_bad_style(client, sample_mp4):
    with sample_mp4.open("rb") as fh:
        r = client.post("/api/jobs", files={"file": ("a.mp4", fh, "video/mp4")}, data={"style": "neon"})
    assert r.status_code == 400


@pytest.mark.parametrize("job_id", ["..%2F..%2Fetc", "0" * 31, "Z" * 32, "a" * 33])
def test_bad_job_ids_rejected(client, job_id):
    assert client.get(f"/api/jobs/{job_id}").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 404


def test_job_dir_validation():
    with pytest.raises(jobs.JobNotFound):
        jobs.job_dir("../" + "a" * 30)


@needs_ffmpeg
def test_full_flow_upload_to_download(client, sample_mp4):
    with sample_mp4.open("rb") as fh:
        r = client.post("/api/jobs", files={"file": ("bài giảng.mp4", fh, "video/mp4")},
                        data={"title": "Bài kiểm thử", "language": "vi", "style": "academic", "slide_count": "6"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "done", job["error"]
    assert job["result"]["title"] == "Bài kiểm thử"
    assert job["result"]["provider"] == "offline"
    assert job["result"]["notices"]  # warns that the offline outline was used
    assert set(job["stage_times"]) >= {"extracting_audio", "transcribing", "analyzing", "generating",
                                       "finalizing", "completed"}

    d = jobs.job_dir(job_id)
    assert not (d / "input.mp4").exists()  # source video deleted after success
    assert not (d / "audio.wav").exists()

    dl = client.get(f"/api/jobs/{job_id}/download")
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"
    assert "attachment" in dl.headers["content-disposition"]

    tr = client.get(f"/api/jobs/{job_id}/transcript").json()
    assert tr["language"] == "vi" and tr["segments"][0]["text"]

    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 409
    assert client.delete(f"/api/jobs/{job_id}").status_code == 204
    assert client.get(f"/api/jobs/{job_id}").status_code == 404


@needs_ffmpeg
def test_retry_reuses_cached_transcript(client, sample_mp4, monkeypatch):
    calls = {"n": 0}
    real = pipeline.build_pptx

    def flaky_build(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "build_pptx", flaky_build)
    with sample_mp4.open("rb") as fh:
        job_id = client.post("/api/jobs", files={"file": ("a.mp4", fh, "video/mp4")}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "boom" in job["error"]

    def must_not_transcribe(*a, **k):
        raise AssertionError("transcript should be reused")

    monkeypatch.setattr(pipeline, "transcribe", must_not_transcribe)
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    assert _wait(client, job_id)["status"] == "done"


def test_password_gate(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, app_password="s3cret"))
    assert client.get("/api/jobs/" + "0" * 32).status_code == 401
    assert client.get("/api/jobs/" + "0" * 32, headers={"X-App-Password": "wrong"}).status_code == 401
    assert client.get("/api/jobs/" + "0" * 32, headers={"X-App-Password": "s3cret"}).status_code == 404


@pytest.fixture
def slow_client(monkeypatch):
    """Transcription that reports progress for up to ~10 s, so a job can be observed mid-flight."""
    def slow_transcribe(audio, language=None, progress=None):
        for i in range(100):
            if progress:
                progress(i * 0.1, 10.0)  # raises JobCancelled once the user cancels
            time.sleep(0.1)
        return Transcript([Segment(0, 1, "Nội dung thử nghiệm đủ dài để tạo dàn ý.")], "vi", 10.0)

    monkeypatch.setattr(pipeline, "transcribe", slow_transcribe)
    with TestClient(main.app) as c:
        yield c


def _wait_stage(client, job_id, stage, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["stage"] == stage:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job never reached {stage}")


@needs_ffmpeg
def test_cancel_running_job_then_retry_resumes(slow_client, sample_mp4):
    with sample_mp4.open("rb") as fh:
        job_id = slow_client.post("/api/jobs", files={"file": ("c.mp4", fh, "video/mp4")}).json()["job_id"]
    job = _wait_stage(slow_client, job_id, "transcribing")
    assert slow_client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    job = _wait(slow_client, job_id)
    assert job["status"] == "cancelled" and "hủy" in job["error"]
    assert slow_client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
    assert slow_client.post(f"/api/jobs/{job_id}/retry").status_code == 202


@needs_ffmpeg
def test_duplicate_upload_follows_active_job(slow_client, sample_mp4):
    ids = []
    for _ in range(2):
        with sample_mp4.open("rb") as fh:
            r = slow_client.post("/api/jobs", files={"file": ("d.mp4", fh, "video/mp4")})
        assert r.status_code == 202
        ids.append(r.json())
    assert ids[1]["job_id"] == ids[0]["job_id"] and ids[1]["duplicate"] is True
    slow_client.post(f"/api/jobs/{ids[0]['job_id']}/cancel")
    _wait(slow_client, ids[0]["job_id"])


@needs_ffmpeg
def test_measured_transcription_progress_is_exposed(slow_client, sample_mp4):
    with sample_mp4.open("rb") as fh:
        job_id = slow_client.post("/api/jobs", files={"file": ("p.mp4", fh, "video/mp4")}).json()["job_id"]
    _wait_stage(slow_client, job_id, "transcribing")
    end = time.time() + 15
    progress = None
    while time.time() < end and not progress:
        progress = slow_client.get(f"/api/jobs/{job_id}").json().get("progress")
        time.sleep(0.2)
    assert progress and progress["stage"] == "transcribing" and progress["total_sec"] == 10.0
    slow_client.post(f"/api/jobs/{job_id}/cancel")
    _wait(slow_client, job_id)


def test_preview_endpoint_without_renderer(client, sample_mp4, monkeypatch):
    monkeypatch.setenv("PREVIEW_ENGINE", "off")
    with sample_mp4.open("rb") as fh:
        job_id = client.post("/api/jobs", files={"file": ("v.mp4", fh, "video/mp4")}).json()["job_id"]
    assert _wait(client, job_id)["status"] == "done"
    body = client.get(f"/api/jobs/{job_id}/preview").json()
    assert body["available"] is False and body["slides"] == []
    assert client.get(f"/api/jobs/{job_id}/preview/1.png").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/preview/0.png").status_code == 404


def test_app_starts_without_gemini_key_or_local_ollama_and_defaults_to_modal_ollama(tmp_path):
    """Fresh process, no LLM_PROVIDER/AI_PROVIDER/GEMINI_API_KEY and no local Ollama (unreachable URL):
    the app starts and the router is selected; it will use Ollama on Modal (no local install needed)."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items() if k not in ("LLM_PROVIDER", "AI_PROVIDER", "GEMINI_API_KEY")}
    env.update(DATA_DIR=str(tmp_path), GEMINI_API_KEY="", PYTHONIOENCODING="utf-8",
               OLLAMA_BASE_URL="http://127.0.0.1:9")
    code = ("import json; from fastapi.testclient import TestClient; from app.main import app\n"
            "with TestClient(app) as c: print(json.dumps(c.get('/api/config').json()['llm']))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.returncode == 0, out.stderr[-2000:]
    llm = json.loads(out.stdout.strip().splitlines()[-1])
    assert llm["provider"] == "modal_ollama" and llm["local"] is False
    assert llm["model"] == "qwen3:4b" and "gemini_configured" not in llm  # Gemini is not in the default route


# ---------------------------------------------------------------- chunked upload

def _start_upload(client, data: bytes, name="a.mp4"):
    r = client.post("/api/uploads", json={"filename": name, "size": len(data)})
    assert r.status_code == 201, r.text
    return r.json()["upload_id"]


def test_chunked_upload_rejects_bad_requests(client):
    assert client.post("/api/uploads", json={"filename": "x.avi", "size": 10}).status_code == 400
    too_big = (main.settings.max_upload_mb + 1) * 1024 * 1024
    assert client.post("/api/uploads", json={"filename": "x.mp4", "size": too_big}).status_code == 413
    assert client.get("/api/uploads/not-an-id").status_code == 404
    uid = _start_upload(client, b"0123456789")
    # Wrong offset: 409 tells the client where to resume.
    r = client.put(f"/api/uploads/{uid}?offset=4", content=b"abc")
    assert r.status_code == 409 and r.json()["detail"]["received"] == 0
    # More bytes than declared.
    assert client.put(f"/api/uploads/{uid}?offset=0", content=b"x" * 11).status_code == 400
    # Finishing an incomplete upload.
    assert client.post(f"/api/uploads/{uid}/finish", json={}).status_code == 409


def test_chunked_upload_resend_is_idempotent(client):
    data = b"abcdefghij"
    uid = _start_upload(client, data)
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data[:4]).json()["received"] == 4
    # Lost response -> client re-sends the same chunk: accepted without duplicating bytes.
    assert client.put(f"/api/uploads/{uid}?offset=0", content=data[:4]).json()["received"] == 4
    st = client.put(f"/api/uploads/{uid}?offset=4", content=data[4:]).json()
    assert st["received"] == 10 and st["complete"]
    assert client.get(f"/api/uploads/{uid}").json()["complete"]


def test_chunked_upload_invalid_video_is_cleaned_up(client):
    data = b"garbage" * 50
    uid = _start_upload(client, data)
    client.put(f"/api/uploads/{uid}?offset=0", content=data)
    before = set(p.name for p in main.settings.jobs_dir.iterdir())
    r = client.post(f"/api/uploads/{uid}/finish", json={})
    assert r.status_code == 400
    assert set(p.name for p in main.settings.jobs_dir.iterdir()) == before
    assert client.get(f"/api/uploads/{uid}").status_code == 404


@needs_ffmpeg
def test_chunked_upload_full_flow(client, sample_mp4):
    data = sample_mp4.read_bytes()
    uid = _start_upload(client, data, "bài giảng chia phần.mp4")
    step = max(1, len(data) // 3)
    offset = 0
    while offset < len(data):
        r = client.put(f"/api/uploads/{uid}?offset={offset}", content=data[offset:offset + step])
        assert r.status_code == 200, r.text
        offset = r.json()["received"]
    r = client.post(f"/api/uploads/{uid}/finish", json={"title": "Chia phần", "language": "vi", "slide_count": 6})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["title"] == "Chia phần"
    assert client.get(f"/api/uploads/{uid}").status_code == 404  # moved into the job folder


def test_youtube_block_suggests_studio_download_and_upload():
    from app.url_import.errors import UPLOAD_HINT, fallback_hint

    assert "YouTube Studio" in fallback_hint("youtube", "bot_check")
    assert fallback_hint("facebook", "bot_check") == UPLOAD_HINT
    assert fallback_hint("youtube", "too_long") == UPLOAD_HINT
