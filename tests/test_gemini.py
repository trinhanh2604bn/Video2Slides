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


def test_rate_limit_is_retried(monkeypatch):
    responses = iter([httpx.Response(429, json={}), httpx.Response(503, json={}), _ok(SAMPLE)])
    pres = _provider(lambda r: next(responses), monkeypatch).generate(_req())
    assert pres.slides


def test_gives_up_after_max_attempts(monkeypatch):
    with pytest.raises(AIError, match="lần thử"):
        _provider(lambda r: httpx.Response(429, json={}), monkeypatch).generate(_req())


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
    assert base.recommend_slide_count(6000, 20000) == 15
