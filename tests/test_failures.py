"""Job-level failure handling: every failure ends in an actionable message and a retry path."""

import time

import pytest
from fastapi.testclient import TestClient

from app import jobs, main, pipeline
from app.ai import AIError
from app.transcribe import Segment, Transcript
from tests.conftest import needs_ffmpeg


@pytest.fixture
def client(monkeypatch):
    def fake_transcribe(audio, language=None, progress=None):
        return Transcript([Segment(i, i + 1.0, f"Câu số {i} nói về kỹ năng làm việc nhóm.") for i in range(5)], "vi", 5.0)

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    with TestClient(main.app) as c:
        yield c


def _submit(client, sample_mp4) -> str:
    with sample_mp4.open("rb") as fh:
        return client.post("/api/jobs", files={"file": ("f.mp4", fh, "video/mp4")}).json()["job_id"]


def _wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


@needs_ffmpeg
def test_llm_failure_is_reported_and_retry_reuses_transcript(client, sample_mp4, monkeypatch):
    class Broken:
        name, is_llm = "gemini", True

        def generate(self, req):
            raise AIError("Gemini tạm thời không khả dụng (HTTP 503).")

    monkeypatch.setattr(pipeline, "get_provider", lambda: Broken())
    job_id = _submit(client, sample_mp4)
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "Gemini" in job["error"]
    assert (jobs.job_dir(job_id) / "transcript.json").exists()  # Whisper result kept for the retry

    monkeypatch.undo()
    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-ran")))
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    assert _wait(client, job_id)["status"] == "done"


@needs_ffmpeg
def test_pptx_render_failure_fails_job_without_partial_download(client, sample_mp4, monkeypatch):
    def broken_build(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "build_pptx", broken_build)
    job_id = _submit(client, sample_mp4)
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "disk full" in job["error"]
    assert client.get(f"/api/jobs/{job_id}/download").status_code == 404


@needs_ffmpeg
def test_invalid_pptx_is_never_offered(client, sample_mp4, monkeypatch):
    from app.pptx_engine import ValidationReport

    monkeypatch.setattr(pipeline, "validate_pptx",
                        lambda *a, **k: ValidationReport(False, 0, 0, ["not a valid pptx (zip) file"]))
    job_id = _submit(client, sample_mp4)
    job = _wait(client, job_id)
    assert job["status"] == "failed" and "không hợp lệ" in job["error"]
    assert not pipeline.output_path(jobs.job_dir(job_id)).exists()


def test_interrupted_jobs_are_marked_failed_after_restart():
    job_id, d = jobs.new_job({"style": "professional"}, "x.mp4")
    jobs.update(job_id, status="running", stage="transcribing")
    jobs.recover_after_restart()
    job = jobs.load(job_id)
    assert job["status"] == "failed" and "Thử lại" in job["error"]
    jobs.delete(job_id)


def test_cancel_queued_job_is_skipped_by_worker():
    job_id, _ = jobs.new_job({"style": "professional"}, "x.mp4")
    jobs.update(job_id, status="queued")
    assert jobs.request_cancel(job_id)["status"] == "cancelled"
    jobs.delete(job_id)


@needs_ffmpeg
def test_ollama_pipeline_unavailable_then_resumes_from_checkpoints(client, sample_mp4, monkeypatch):
    """MOCK Ollama (tests/fake_ollama.py): stages, error code, retry without Whisper, LLM checkpoints."""
    import httpx

    from app.ai.ollama import OllamaProvider
    from app.ai.ollama_client import OllamaClient
    from tests.fake_ollama import FakeOllama

    fake = FakeOllama()
    state = {"up": False, "fail_second_batch": True, "batches": 0}

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("refused")
        if request.url.path == "/api/chat" and "slide_id" in request.content.decode():
            state["batches"] += 1
            if state["fail_second_batch"] and state["batches"] == 2:
                return httpx.Response(500, json={"error": "llama runner process has terminated"})
        return fake.handle(request)

    def provider():
        return OllamaProvider(client=OllamaClient(base_url="http://ollama.test", transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(pipeline, "get_provider", provider)
    job_id = _submit(client, sample_mp4)
    job = _wait(client, job_id)
    assert job["status"] == "failed" and job["error_code"] == "ollama_unavailable"
    assert "Ollama" in job["error"]

    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-ran")))
    state["up"] = True
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    job = _wait(client, job_id)
    assert job["status"] == "failed" and job["error_code"] == "model_load_failed"
    assert any((jobs.job_dir(job_id) / "llm_cache").iterdir())  # outline + first batch checkpointed

    state["fail_second_batch"] = False
    n = len(fake.chats)
    assert client.post(f"/api/jobs/{job_id}/retry").status_code == 202
    job = _wait(client, job_id)
    assert job["status"] == "done", job["error"]
    assert fake.steps()[n:] == ["slides"]  # only the failed batch was generated again
    assert "writing_slides" in job["stage_times"] and job["result"]["provider"] == "ollama"
    assert job["result"]["llm"]["model"] == "qwen3:4b" and job["result"]["llm"]["cache_hits"] >= 2
