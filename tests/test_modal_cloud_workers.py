"""Request handling of the Modal LLM and media workers' web APIs, exercised locally without a GPU.

MOCKED: the GPU classes and Modal Dict/Volume are replaced by fakes; this checks validation, limits, auth-free
health and the no-GPU-on-health rule, not GPU execution.
"""

import ast
import hashlib
import json
from pathlib import Path

import pytest

modal = pytest.importorskip("modal")  # optional "worker" extra

import infrastructure.modal_llm_worker as llm  # noqa: E402
import infrastructure.modal_media_worker as media  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_modal_worker import FakeCall, FakeDict  # noqa: E402


class FakeVolume:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def reload(self):
        pass


class Spawner:
    def __init__(self):
        self.spawned = []

    def spawn(self, *args):
        self.spawned.append(args)
        return FakeCall(len(self.spawned))


@pytest.mark.parametrize("mod", [llm, media])
def test_modules_have_no_postponed_annotations(mod):
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert not any(a.name == "annotations" for n in futures for a in n.names)


def test_ollama_daemon_is_bound_to_loopback_only():
    assert llm.OLLAMA_ADDR.startswith("127.0.0.1:")
    assert "0.0.0.0" not in Path(llm.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------- LLM worker

VALID_CHAT = {"model": "qwen3:4b", "messages": [{"role": "user", "content": "xin chào"}],
              "options": {"num_ctx": 8192, "temperature": 0.2, "num_predict": 500}, "think": False,
              "format": {"type": "object"}, "keep_alive": "5m"}


@pytest.fixture
def llm_client(monkeypatch, tmp_path):
    spawner = Spawner()
    monkeypatch.setattr(llm, "request_index", FakeDict())
    monkeypatch.setattr(llm, "models_volume", FakeVolume())
    monkeypatch.setattr(llm, "MODELS_DIR", str(tmp_path))
    monkeypatch.setattr(llm, "OllamaWorker", lambda: type("W", (), {"generate_content": spawner})())
    return TestClient(llm.api._raw_f_()), spawner, tmp_path


def test_llm_validate_request_maps_only_whitelisted_fields():
    out = llm.validate_request({**VALID_CHAT, "options": {**VALID_CHAT["options"], "seed": 3}, "raw": True})
    assert out["options"] == {"num_ctx": 8192, "temperature": 0.2, "num_predict": 500}
    assert out["stream"] is False and "raw" not in out


@pytest.mark.parametrize("change", [
    {"model": "qwen3:14b"},                                            # never another model
    {"messages": []},
    {"messages": [{"role": "tool", "content": "x"}]},
    {"options": {"num_ctx": 65536}},
    {"options": {"num_predict": 100000}},
    {"think": "yes"},
    {"keep_alive": "forever"},
    {"messages": [{"role": "user", "content": "x" * (300 * 1024)}]},   # size limit
])
def test_llm_invalid_requests_are_rejected_before_any_gpu(llm_client, change):
    client, spawner, _ = llm_client
    r = client.post("/v1/chat", json={"request_id": "req-00000001", "request": {**VALID_CHAT, **change}})
    assert r.status_code in (413, 422)
    assert spawner.spawned == []


def test_llm_health_reports_model_readiness_without_gpu(llm_client):
    client, spawner, tmp = llm_client
    assert client.get("/v1/health").json()["model_ready"] is False
    Path(llm.marker_path()).write_text(json.dumps({"model": "qwen3:4b", "quantization": "Q4_K_M"}))
    h = client.get("/v1/health").json()
    assert h["model_ready"] is True and h["model_info"]["quantization"] == "Q4_K_M"
    assert h["limits"]["max_containers"] == 1 and spawner.spawned == []


def test_llm_chat_spawns_once_and_deduplicates(llm_client):
    client, spawner, _ = llm_client
    body = {"request_id": "req-00000001", "request": VALID_CHAT}
    a = client.post("/v1/chat", json=body).json()
    b = client.post("/v1/chat", json=body).json()
    assert a["deduplicated"] is False and b == {"call_id": a["call_id"], "deduplicated": True}
    assert len(spawner.spawned) == 1


# ---------------------------------------------------------------------- media worker

@pytest.fixture
def media_client(monkeypatch, tmp_path):
    spawner, transcriber = Spawner(), Spawner()
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(media, "media_volume", FakeVolume())
    monkeypatch.setattr(media, "progress", FakeDict())
    monkeypatch.setattr(media, "preprocess", spawner)
    monkeypatch.setattr(media, "WhisperWorker", lambda: type("W", (), {"transcribe": transcriber})())
    return TestClient(media.api._raw_f_()), spawner, transcriber, tmp_path


def test_media_chunked_upload_complete_and_delete(media_client):
    client, spawner, transcriber, tmp = media_client
    assert client.get("/v1/health").json()["ok"] and spawner.spawned == [] and transcriber.spawned == []
    uid = client.post("/v1/uploads").json()["upload_id"]
    data = b"\x00\x00\x00\x18ftypisom" + b"x" * 5000
    assert client.put(f"/v1/uploads/{uid}/chunks/0", content=data[:3000]).status_code == 200
    assert client.put(f"/v1/uploads/{uid}/chunks/1", content=data[3000:]).status_code == 200
    r = client.post(f"/v1/uploads/{uid}/complete", json={"sha256": hashlib.sha256(data).hexdigest(),
                                                          "size": len(data), "chunks": 2, "params": {}})
    assert r.status_code == 200 and spawner.spawned[0][0] == uid
    assert client.post("/v1/transcribe", json={"upload_id": uid, "params": {"model": "base"}}).status_code == 200
    assert client.delete(f"/v1/uploads/{uid}").json() == {"deleted": True}
    assert not (tmp / "up" / uid).exists()


@pytest.mark.parametrize("path", ["/v1/uploads/../../etc/chunks/0", "/v1/uploads/ABC/chunks/0",
                                  "/v1/uploads/0123/chunks/0"])
def test_media_rejects_client_supplied_paths(media_client, path):
    client = media_client[0]
    assert client.put(path, content=b"x").status_code in (404, 422)


def test_media_rejects_oversized_chunks(media_client, monkeypatch):
    client = media_client[0]
    monkeypatch.setattr(media, "MAX_CHUNK_BYTES", 10)
    uid = client.post("/v1/uploads").json()["upload_id"]
    assert client.put(f"/v1/uploads/{uid}/chunks/0", content=b"x" * 11).status_code == 413


def test_media_preprocess_verifies_hash_and_validates(monkeypatch, tmp_path):
    """preprocess() itself: a corrupt upload is refused before FFmpeg runs."""
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(media, "media_volume", FakeVolume())
    uid = "0" * 32
    (tmp_path / "up" / uid / "chunks").mkdir(parents=True)
    (tmp_path / "up" / uid / "chunks" / "00000").write_bytes(b"abc")
    out = media.preprocess.local(uid, "0" * 64, 3, 1, {})
    assert out["ok"] is False and out["error_code"] == "upload_corrupt"


def test_transcribe_refuses_without_cuda(monkeypatch, tmp_path):
    """On this test machine CTranslate2 sees no CUDA device: the worker must refuse, not run on CPU."""
    pytest.importorskip("ctranslate2")
    import ctranslate2

    if ctranslate2.get_cuda_device_count() > 0:
        pytest.skip("a CUDA device is present here")
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path))
    out = media.WhisperWorker().transcribe.local("0" * 32, {"model": "base"})
    assert out["ok"] is False and out["error_code"] == "cuda_unavailable"


def test_ollama_server_logs_never_go_to_an_unread_pipe():
    """Regression: an unread stderr PIPE filled up after ~30 requests and blocked `ollama serve` (long decks hung)."""
    src = Path(llm.__file__).read_text(encoding="utf-8")
    start = src.index("def _start_ollama")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "subprocess.PIPE" not in body and "OLLAMA_LOG" in body
