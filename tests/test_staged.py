"""Long videos: Gemini writes in validated stages; each stage falls back to Ollama on Modal on its own.

MOCKED: Gemini is an httpx MockTransport answering from tests/fake_ollama.py, Ollama on Modal is
tests/fake_modal.py. No real model runs here.
"""

import json

import httpx
import pytest

from app.ai import gemini
from app.ai.base import AIError, OutlineRequest
from app.ai.gemini_chat import GeminiChatClient, to_gemini_schema
from app.ai.router import CircuitBreaker, RoutingProvider
from app.transcribe import Segment, Transcript
from tests.fake_modal import FakeModalLLM, use_settings
from tests.fake_ollama import FakeOllama, _step


class FakeGemini:
    """Answers generateContent with the fake Ollama's structured answers; ``fail`` scripts failures per call."""

    def __init__(self):
        self.ollama = FakeOllama()
        self.calls: list[str] = []
        self.fail: dict[int, httpx.Response] = {}  # call index (1-based) -> response

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema = body["generationConfig"]["responseSchema"]
        step = _step(schema)
        self.calls.append(step)
        n = len(self.calls)
        if n in self.fail:
            return self.fail[n]
        pseudo = {"format": schema, "messages": [{"role": "user", "content": body["contents"][-1]["parts"][0]["text"]}]}
        answer = getattr(self.ollama, f"answer_{step}")(pseudo)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]},
                                                         "finishReason": "STOP"}],
                                         "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500}})

    def client(self) -> GeminiChatClient:
        return GeminiChatClient(api_key="k", model="gm", client=httpx.Client(transport=httpx.MockTransport(self.handle)))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    use_settings(monkeypatch, gemini_api_key="k", gemini_model="gm", data_dir=tmp_path / "data",
                 modal_poll_interval_sec=0, llm_staged_min_sec=900)
    return tmp_path


def _long_transcript(minutes: int = 20) -> Transcript:
    segs = [Segment(i * 10.0, i * 10.0 + 9.0, f"Câu số {i} giải thích một ý của bài giảng về quản lý dự án.")
            for i in range(minutes * 6)]
    return Transcript(segs, "vi", minutes * 60.0)


def _router(g: FakeGemini, m: FakeModalLLM, tmp):
    return RoutingProvider(circuit=CircuitBreaker(tmp / "c.json"), modal_factory=m.provider,
                           gemini_chat_factory=g.client,
                           gemini_factory=lambda: (_ for _ in ()).throw(AssertionError("single-call path used")))


def _req(tmp, **kw):
    kw.setdefault("slide_count", 8)
    return OutlineRequest(transcript=_long_transcript(), language="vi", cache_dir=tmp / "job", **kw)


def test_schema_conversion_keeps_constraints():
    s = to_gemini_schema({"type": "object", "properties": {"a": {"type": "array", "minItems": 2, "maxItems": 3,
                                                                  "items": {"type": "string", "enum": ["x"]}}},
                          "required": ["a"]})
    assert s["type"] == "OBJECT" and s["properties"]["a"]["items"] == {"type": "STRING", "enum": ["x"]}
    assert s["properties"]["a"]["minItems"] == 2 and s["required"] == ["a"] and s["propertyOrdering"] == ["a"]


def test_long_video_is_written_by_gemini_in_stages_without_modal(env):
    g, m = FakeGemini(), FakeModalLLM()
    r = _router(g, m, env)
    pres = r.generate(_req(env))
    assert len(pres.slides) == 8
    assert r.route["mode"] == "staged" and r.route["providers_used"] == ["gemini"]
    assert "outline" in g.calls and g.calls.count("slides") >= 3 and m.submitted == [] and m.health_calls == 0
    entries = [json.loads(f.read_text(encoding="utf-8")) for f in (env / "job" / "llm_cache").glob("*.json")]
    assert entries and {e["provider"] for e in entries} == {"gemini"}


def test_gemini_outage_mid_deck_keeps_finished_units_and_continues_on_modal(env):
    g, m = FakeGemini(), FakeModalLLM()
    g.fail = {3: httpx.Response(503, json={}), 4: httpx.Response(503, json={})}  # 2nd slide batch fails twice
    statuses = []
    r = _router(g, m, env)
    pres = r.generate(_req(env, status=statuses.append))
    assert len(pres.slides) == 8
    assert r.route["providers_used"] == ["gemini", "modal_ollama"]  # mixed provenance recorded
    assert r.route["fallback_reason"] == "server"
    assert "Gemini không khả dụng, đang chuyển sang Ollama trên Modal." in statuses
    modal_steps = [_step(x["format"]) for x in m.submitted]
    assert "outline" not in modal_steps and set(modal_steps) == {"slides"}  # outline + batch 1 kept from Gemini
    providers = {json.loads(f.read_text(encoding="utf-8"))["provider"] for f in (env / "job" / "llm_cache").glob("*.json")}
    assert providers == {"gemini", "modal_ollama"}
    info = r.describe()
    assert info["unit_fallback"]["kind"] == "server" and info["providers_used"] == ["gemini", "modal_ollama"]


def test_invalid_gemini_unit_is_repaired_then_redone_on_modal(env):
    g, m = FakeGemini(), FakeModalLLM()
    bad = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{\"slides\": []}"}]}}]})
    g.fail = {i: bad for i in range(2, 6)}  # first slide batch: initial answer + 2 repairs all invalid
    r = _router(g, m, env)
    assert len(r.generate(_req(env)).slides) == 8
    assert r.route["fallback_reason"] == "invalid_output" and "modal_ollama" in r.route["providers_used"]


def test_safety_refusal_mid_deck_stops_without_fallback(env):
    g, m = FakeGemini(), FakeModalLLM()
    g.fail = {2: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})}
    with pytest.raises(AIError) as exc:
        _router(g, m, env).generate(_req(env))
    assert exc.value.code == "gemini_safety" and m.submitted == []


def test_retry_after_failure_resumes_from_checkpoints(env):
    g, m = FakeGemini(), FakeModalLLM()
    m.fail = {"error_code": "startup_failed", "error": "x", "retryable": False}
    g.fail = {3: httpx.Response(401, json={})}
    with pytest.raises(AIError) as exc:
        _router(g, m, env).generate(_req(env))
    assert exc.value.code == "all_providers_failed"
    done_before = len(g.calls)
    m.fail = None
    r = _router(g, m, env)  # same job: pinned to Modal, validated Gemini units reused from the cache
    pres = r.generate(_req(env))
    assert len(pres.slides) == 8 and len(g.calls) == done_before
    assert r.route["fallback_reason"] == "sticky"


def test_short_video_keeps_the_single_call_gemini_path(env, monkeypatch):
    called = []

    class G:
        used_model = "gm"

        def generate(self, req):
            called.append(1)
            from tests.sample_outline import SAMPLE
            from app.ai.schema import Presentation
            return Presentation.model_validate(SAMPLE)

        def describe(self):
            return {}

    r = RoutingProvider(circuit=CircuitBreaker(env / "c.json"), gemini_factory=G, modal_factory=FakeModalLLM().provider)
    t = Transcript([Segment(0, 5, "Xin chào")], "vi", 300)
    r.generate(OutlineRequest(transcript=t, language="vi", slide_count=6, cache_dir=env / "job"))
    assert called == [1] and r.route["mode"] == "auto"


def test_long_video_without_gemini_uses_the_staged_route_and_reuses_its_cache(env, monkeypatch):
    g, m = FakeGemini(), FakeModalLLM()
    m.fail = {"error_code": "startup_failed", "error": "x", "retryable": False}
    g.fail = {4: httpx.Response(401, json={})}  # chunk + outline + batch 1 done by Gemini, then a bad key
    with pytest.raises(AIError):
        _router(g, m, env).generate(_req(env))
    m.fail = None
    use_settings(monkeypatch, gemini_api_key="", gemini_model="gm", data_dir=env / "data", modal_poll_interval_sec=0,
                 llm_staged_min_sec=900)
    r = _router(g, m, env)
    pres = r.generate(_req(env))
    assert len(pres.slides) == 8 and r.route["mode"] == "staged" and r.route["fallback_reason"] == "missing_key"
    assert "outline" not in [_step(x["format"]) for x in m.submitted]  # Gemini's validated outline was reused
