"""LLM routing (LLM_PROVIDER=auto): Gemini first, Ollama on Modal as automatic fallback.

MOCKED: Gemini is an httpx MockTransport, Ollama on Modal is tests/fake_modal.py (no GPU, no model).
Live checks against the real Modal worker are in scripts/modal_llm_live_check.py.
"""

import json
import time

import httpx
import pytest

from app.ai import gemini
from app.ai.base import AIError, OutlineRequest
from app.ai.modal_ollama import ModalOllamaClient
from app.ai.router import CircuitBreaker, RoutingProvider
from app.jobs import JobCancelled
from app.transcribe import Segment, Transcript
from tests.fake_modal import FakeModalLLM, use_settings
from tests.sample_outline import SAMPLE


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    use_settings(monkeypatch, gemini_api_key="test-key", gemini_model="gm", gemini_fallback_models=(),
                 data_dir=tmp_path / "data", modal_poll_interval_sec=0, llm_circuit_cooldown_sec=600,
                 llm_circuit_failure_threshold=2)
    return tmp_path


class Gemini:
    """Scripted Gemini endpoint: a list of responses (or exceptions) used in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        r = self.responses[min(len(self.requests), len(self.responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    def factory(self):
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return lambda: gemini.GeminiProvider(client=client)


def ok(payload=SAMPLE):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def _transcript():
    return Transcript([Segment(i * 5.0, i * 5.0 + 4.5, f"Câu số {i} về quản lý thời gian.") for i in range(12)], "vi", 60)


def _req(tmp_path=None, **kw):
    kw.setdefault("slide_count", 6)
    return OutlineRequest(transcript=_transcript(), language="vi", cache_dir=tmp_path, **kw)


def _router(g: Gemini | None, fake: FakeModalLLM, tmp_path):
    return RoutingProvider(circuit=CircuitBreaker(tmp_path / "circuit.json"),
                           gemini_factory=g.factory() if g else None, modal_factory=fake.provider)


# ---------------------------------------------------------------------- routing decisions

def test_missing_key_goes_straight_to_modal_without_a_gemini_request(env, monkeypatch):
    use_settings(monkeypatch, gemini_api_key="", data_dir=env / "data", modal_poll_interval_sec=0)
    g, fake = Gemini(ok()), FakeModalLLM()
    statuses = []
    r = _router(g, fake, env)
    pres = r.generate(_req(env / "job", status=statuses.append))
    assert pres.slides and g.requests == []
    assert r.used_provider == "modal_ollama" and r.route["fallback_reason"] == "missing_key"
    assert r.route["attempted"] == ["modal_ollama"]
    assert statuses[0] == "Đang dùng Ollama trên Modal (không có khóa Gemini)."
    assert "Đang tạo nội dung slide bằng Qwen3." in statuses


def test_gemini_success_never_invokes_modal(env):
    g, fake = Gemini(ok()), FakeModalLLM()
    r = _router(g, fake, env)
    pres = r.generate(_req(env / "job"))
    assert pres.presentation_title == SAMPLE["presentation_title"]
    assert len(g.requests) == 1 and fake.submitted == [] and fake.health_calls == 0
    assert r.used_provider == "gemini" and r.route["fallback_reason"] is None
    assert r.describe()["provider"] == "gemini"


@pytest.mark.parametrize("response,kind", [
    (httpx.Response(401, json={"error": {"message": "bad"}}), "auth"),
    (httpx.Response(400, json={"error": {"message": "API key not valid.", "details": [{"reason": "API_KEY_INVALID"}]}}),
     "auth"),
    (httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "details": [
        {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}), "quota"),
    (httpx.Response(404, json={"error": {"message": "model not found"}}), "config"),
])
def test_non_retryable_gemini_failures_fall_back_after_one_request(env, response, kind):
    g, fake = Gemini(response), FakeModalLLM()
    r = _router(g, fake, env)
    assert r.generate(_req(env / "job")).slides
    assert len(g.requests) == 1  # never retried with the same invalid credentials / exhausted quota
    assert r.used_provider == "modal_ollama" and r.route["fallback_reason"] == kind
    assert r.route["circuit"]["open"] is True  # opens at once for these kinds
    if kind == "config":
        assert r.route["warnings"]


def test_transient_errors_are_retried_once_then_fall_back(env):
    g, fake = Gemini(httpx.Response(500, json={}), httpx.Response(502, json={})), FakeModalLLM()
    r = _router(g, fake, env)
    assert r.generate(_req(env / "job")).slides
    assert len(g.requests) == 2 and r.route["fallback_reason"] == "server"
    assert r.route["circuit"]["open"] is False and r.route["circuit"]["consecutive_failures"] == 1


def test_timeout_falls_back(env):
    g, fake = Gemini(httpx.ReadTimeout("slow")), FakeModalLLM()
    r = _router(g, fake, env)
    assert r.generate(_req(env / "job")).slides
    assert len(g.requests) == 2 and r.route["fallback_reason"] == "timeout"


def test_invalid_json_gets_two_repairs_then_falls_back(env):
    bad = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"slides": []}'}]}}]})
    g, fake = Gemini(bad), FakeModalLLM()
    r = _router(g, fake, env)
    assert r.generate(_req(env / "job")).slides
    assert len(g.requests) == 3  # first answer + 2 targeted repairs, no more
    assert len(g.requests[2]["contents"]) == 3  # repair turn carries the rejected answer
    assert r.route["fallback_reason"] == "invalid_output"


def test_cancellation_stops_the_job_and_never_falls_back(env):
    g, fake = Gemini(httpx.Response(503, json={})), FakeModalLLM()
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 1:
            raise JobCancelled("job")

    with pytest.raises(JobCancelled):
        _router(g, fake, env).generate(_req(env / "job", check_cancel=cancel))
    assert fake.submitted == [] and fake.health_calls == 0


def test_safety_refusal_is_not_bypassed_with_another_provider(env):
    g, fake = Gemini(httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})), FakeModalLLM()
    with pytest.raises(AIError) as exc:
        _router(g, fake, env).generate(_req(env / "job"))
    assert exc.value.code == "gemini_safety" and fake.submitted == []


def test_application_bad_request_is_reported_not_hidden(env):
    g, fake = Gemini(httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "schema"}})), \
        FakeModalLLM()
    with pytest.raises(AIError) as exc:
        _router(g, fake, env).generate(_req(env / "job"))
    assert exc.value.code == "gemini_bad_request" and fake.submitted == []


def test_both_providers_failing_gives_a_clear_error_with_safe_statuses(env):
    g = Gemini(httpx.Response(401, json={"error": {"message": "secret detail AIza..."}}))
    fake = FakeModalLLM(model_ready=False)
    with pytest.raises(AIError) as exc:
        _router(g, fake, env).generate(_req(env / "job"))
    msg = str(exc.value)
    assert exc.value.code == "all_providers_failed"
    assert "Gemini: khóa Gemini không hợp lệ" in msg and "setup_model" in msg
    assert "test-key" not in msg and "AIza" not in msg


# ---------------------------------------------------------------------- stability: sticky job, circuit breaker

def test_job_stays_on_modal_after_an_eligible_gemini_failure(env):
    g, fake = Gemini(httpx.Response(503, json={}), httpx.Response(503, json={})), FakeModalLLM()
    _router(g, fake, env).generate(_req(env / "job"))
    g2 = Gemini(ok())
    r = RoutingProvider(circuit=CircuitBreaker(env / "other.json"), gemini_factory=g2.factory(),
                        modal_factory=fake.provider)
    r.generate(_req(env / "job"))  # retry of the same job
    assert g2.requests == [] and r.route["fallback_reason"] == "sticky"


def test_circuit_opens_after_repeated_failures_and_closes_after_cooldown(env, monkeypatch):
    fake = FakeModalLLM()
    circuit = CircuitBreaker(env / "circuit.json")
    for i in range(2):  # two jobs with transient failures -> threshold reached
        g = Gemini(httpx.Response(500, json={}))
        RoutingProvider(circuit=circuit, gemini_factory=g.factory(), modal_factory=fake.provider) \
            .generate(_req(env / f"job{i}"))
    assert circuit.state()["open"]

    g = Gemini(ok())
    r = RoutingProvider(circuit=circuit, gemini_factory=g.factory(), modal_factory=fake.provider)
    r.generate(_req(env / "job-next"))
    assert g.requests == [] and r.route["fallback_reason"] == "circuit_open"  # no probe per job

    real = time.time
    monkeypatch.setattr("app.ai.router.time.time", lambda: real() + 601)  # cooldown expired
    r = RoutingProvider(circuit=circuit, gemini_factory=g.factory(), modal_factory=fake.provider)
    r.generate(_req(env / "job-later"))
    assert r.used_provider == "gemini" and not circuit.state()["consecutive_failures"]  # success resets


def test_new_key_or_model_re_evaluates_the_circuit(env, monkeypatch):
    circuit = CircuitBreaker(env / "circuit.json")
    circuit.record_failure("auth")
    assert circuit.state()["open"]
    use_settings(monkeypatch, gemini_api_key="new-key", data_dir=env / "data", modal_poll_interval_sec=0)
    assert not circuit.state()["open"]
    assert "new-key" not in (env / "circuit.json").read_text()  # only a hash of the configuration


# ---------------------------------------------------------------------- Modal transport details

def test_modal_health_does_not_start_gpu_and_model_missing_is_explicit(env):
    fake = FakeModalLLM(model_ready=False)
    with pytest.raises(AIError) as exc:
        fake.provider().generate(_req(env / "job"))
    assert exc.value.code == "modal_model_missing" and fake.submitted == []


def test_modal_requests_carry_proxy_auth_and_mapped_ollama_parameters(env):
    fake = FakeModalLLM()
    p = fake.provider()
    p.generate(_req(env / "job"))
    first = fake.submitted[0]
    assert first["model"] == "qwen3:4b" and first["options"]["num_ctx"] == 8192
    assert first["think"] is False and first["format"]["type"] == "object"
    assert "<transcript>" in first["messages"][-1]["content"]  # transcript text, never the video
    info = p.describe()
    assert info["provider"] == "modal_ollama" and info["modal"]["gpu"] == "NVIDIA L4"
    assert info["modal"]["cold_starts"] == 1 and info["modal"]["estimated_gpu_cost_usd_excl_idle"] > 0


def test_modal_rejects_wrong_token(env):
    fake = FakeModalLLM()
    client = ModalOllamaClient(base_url="https://llm.modal.test", token_id="x", token_secret="y",
                               client=httpx.Client(transport=fake.transport()))
    with pytest.raises(AIError) as exc:
        client.health()
    assert exc.value.code == "modal_auth"


def test_cancel_during_remote_generation_cancels_the_modal_call(env):
    fake = FakeModalLLM(pending_polls=10_000)
    polls = []

    def cancel():
        polls.append(1)
        if len(polls) > 3:
            raise JobCancelled("job")

    with pytest.raises(JobCancelled):
        fake.provider().generate(_req(env / "job", check_cancel=cancel))
    assert fake.cancelled == ["fc-000000000001"]


def test_cpu_only_inference_is_refused(env):
    fake = FakeModalLLM()
    fake.fail = {"error_code": "gpu_not_used", "error": "cpu", "retryable": False}
    with pytest.raises(AIError) as exc:
        fake.provider().generate(_req(env / "job"))
    assert exc.value.code == "modal_gpu_not_used"


# ---------------------------------------------------------------------- cache provenance

def test_cache_records_provider_and_is_not_shared_with_local_ollama(env):
    from app.ai.ollama import OllamaProvider
    from app.ai.ollama_client import OllamaClient

    fake = FakeModalLLM()
    fake.provider().generate(_req(env / "job"))
    entries = [json.loads(f.read_text(encoding="utf-8")) for f in (env / "job" / "llm_cache").glob("*.json")]
    assert entries and all(e["provider"] == "modal_ollama" and e["model"] == "qwen3:4b" for e in entries)

    again = fake.provider()
    again.generate(_req(env / "job"))
    assert again.stats.cache_hits >= 2 and again.stats.requests == 0  # resumed from validated checkpoints

    local = OllamaProvider(client=OllamaClient(base_url="http://ollama.test", transport=fake.ollama.transport()))
    local.generate(_req(env / "job"))
    assert local.stats.cache_hits == 0  # another provider never reuses these answers
