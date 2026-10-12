import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, pipeline
from app.transcribe import Segment, Transcript
from tests.conftest import needs_ffmpeg


@pytest.fixture
def client(monkeypatch):
    def fake_transcribe(audio, language=None):
        segs = [Segment(i * 1.0, i * 1.0 + 1, f"Câu thử nghiệm số {i} về quản lý thời gian hiệu quả.") for i in range(6)]
        return Transcript(segs, "vi", 6.0)

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    with TestClient(main.app) as c:
        yield c


def _wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_health_and_config(client):
    assert client.get("/api/health").status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["max_upload_mb"] == 80
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
