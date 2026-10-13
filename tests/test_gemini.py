import json

import httpx
import pytest

from app.ai import base, gemini
from app.ai.base import AIError, OutlineRequest
from app.transcribe import Segment, Transcript
from tests.sample_outline import SAMPLE


def _req(**kw):
    t = Transcript([Segment(0, 5, "Xin chào"), Segment(5, 10, "Nội dung chính")], "vi", 10)
    return OutlineRequest(transcript=t, language="vi", slide_count=None, **kw)


def _ok(payload):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def _provider(handler, monkeypatch):
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return gemini.GeminiProvider(api_key="test-key", model="test-model", client=client)


def test_success_and_key_sent_in_header(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = json.loads(request.content)
        return _ok(SAMPLE)

    pres = _provider(handler, monkeypatch).generate(_req())
    assert pres.presentation_title == SAMPLE["presentation_title"]
    assert seen["key"] == "test-key"
    assert "test-key" not in seen["url"]
    assert "test-model" in seen["url"]
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert "[0.0] Xin chào" in seen["body"]["contents"][0]["parts"][0]["text"]


def test_title_hint_overrides(monkeypatch):
    pres = _provider(lambda r: _ok(SAMPLE), monkeypatch).generate(_req(title_hint="Tiêu đề riêng"))
    assert pres.presentation_title == "Tiêu đề riêng"


def test_invalid_json_triggers_repair_turn(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"slides": []}'}]}}]})
        return _ok(SAMPLE)

    pres = _provider(handler, monkeypatch).generate(_req())
    assert len(calls) == 2
    assert len(calls[1]["contents"]) == 3  # user, model (bad output), user (repair request)
    assert calls[1]["contents"][1]["role"] == "model"
    assert pres.slides


def test_short_rate_limit_is_retried_once(monkeypatch):
    responses = iter([httpx.Response(429, json={}), _ok(SAMPLE)])
    pres = _provider(lambda r: next(responses), monkeypatch).generate(_req())
    assert pres.slides


def test_gives_up_after_two_request_attempts(monkeypatch):
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(429, json={})

    with pytest.raises(gemini.GeminiError) as exc:
        _provider(handler, monkeypatch).generate(_req())
    assert exc.value.kind == "rate_limit" and len(calls) == 2


def test_overloaded_model_switches_to_fallback_model_within_budget(monkeypatch):
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(503, json={}) if len(seen) == 1 else _ok(SAMPLE)

    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = gemini.GeminiProvider(api_key="k", model="main-model", client=client, fallback_models=("lite-model",))
    assert p.generate(_req()).slides
    assert "main-model" in seen[0] and "lite-model" in seen[1] and p.used_model == "lite-model"


def test_classifies_quota_exhaustion_vs_rate_limit():
    exhausted = httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED", "details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
    per_minute = httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"details": [{
        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]}]}})
    assert gemini.classify_http_error(exhausted, "m").kind == "quota"
    e = gemini.classify_http_error(per_minute, "m")
    assert e.kind == "rate_limit" and e.retry_after == 7


def test_classifies_invalid_key_sent_as_400():
    resp = httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid.",
                                               "details": [{"reason": "API_KEY_INVALID"}]}})
    assert gemini.classify_http_error(resp, "m").kind == "auth"


def test_safety_block_is_a_refusal_not_a_retryable_error(monkeypatch):
    blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    calls = []

    def handler(r):
        calls.append(1)
        return blocked

    with pytest.raises(gemini.GeminiError) as exc:
        _provider(handler, monkeypatch).generate(_req())
    assert exc.value.kind == "safety" and len(calls) == 1


def test_long_retry_after_falls_back_promptly(monkeypatch):
    slept = []
    monkeypatch.setattr(gemini.time, "sleep", lambda s: slept.append(s))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "120"}, json={})))
    with pytest.raises(gemini.GeminiError):
        gemini.GeminiProvider(api_key="k", model="m", client=client, fallback_models=()).generate(_req())
    assert slept == []  # does not wait 2 minutes when a fallback exists


def test_bad_key_fails_fast(monkeypatch):
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(403, json={"error": {"message": "denied"}})

    with pytest.raises(AIError, match="GEMINI_API_KEY"):
        _provider(handler, monkeypatch).generate(_req())
    assert len(calls) == 1


def test_missing_key_raises():
    with pytest.raises(AIError, match="GEMINI_API_KEY"):
        gemini.GeminiProvider(api_key="")


def test_recommend_slide_count_bounds():
    assert base.recommend_slide_count(30, 50) == 6
    assert base.recommend_slide_count(600, 1500) == 14
    assert base.recommend_slide_count(900, 2070) == 16      # 15 min
    assert base.recommend_slide_count(5400, 12420) == 35    # 90 min: ~1 more slide per 4 extra minutes
    assert base.recommend_slide_count(6000, 20000) == base.MAX_SLIDES == 40
