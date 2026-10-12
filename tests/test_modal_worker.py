"""Request handling of the Modal GPU worker's web API, exercised locally without a GPU.

Regression: with ``from __future__ import annotations`` the request model defined inside
``api()`` could not be resolved, so FastAPI treated ``body`` as a query parameter and every
valid ``POST /v1/generate`` was answered with 422.
"""

import ast
from pathlib import Path

import pytest

modal = pytest.importorskip("modal")  # optional "worker" extra

import infrastructure.modal_image_worker as worker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VALID = {"prompt": "a lighthouse at dawn", "negative_prompt": "text", "width": 1344, "height": 768,
         "steps": 25, "guidance": 6.0, "seed": 1}


class FakeDict(dict):
    def put(self, key, value, skip_if_exists=False):
        if skip_if_exists and key in self:
            return False
        self[key] = value
        return True


class FakeCall:
    def __init__(self, n):
        self.object_id = f"fc-{n:012d}"
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeWorker:
    spawned: list = []

    def __init__(self):
        self.generate = self

    def spawn(self, params):
        FakeWorker.spawned.append(params)
        return FakeCall(len(FakeWorker.spawned))


@pytest.fixture
def client(monkeypatch):
    FakeWorker.spawned = []
    monkeypatch.setattr(worker, "request_index", FakeDict())
    monkeypatch.setattr(worker, "SDXLWorker", FakeWorker)
    return TestClient(worker.api._raw_f_())


def test_module_has_no_postponed_annotations():
    tree = ast.parse(Path(worker.__file__).read_text(encoding="utf-8"))
    futures = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert not any(a.name == "annotations" for n in futures for a in n.names)


def test_health_does_not_spawn_gpu(client):
    r = client.get("/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert FakeWorker.spawned == []


def test_valid_body_is_parsed_and_spawns_once(client):
    r = client.post("/v1/generate", json={"request_id": "job1-slide-01", "params": VALID})
    assert r.status_code == 200, r.text
    assert r.json() == {"call_id": "fc-000000000001", "deduplicated": False}
    assert FakeWorker.spawned[0]["prompt"] == "a lighthouse at dawn"


def test_duplicate_request_id_is_deduplicated(client):
    body = {"request_id": "job1-slide-01", "params": VALID}
    first = client.post("/v1/generate", json=body).json()
    second = client.post("/v1/generate", json=body).json()
    assert second == {"call_id": first["call_id"], "deduplicated": True}
    assert len(FakeWorker.spawned) == 1


@pytest.mark.parametrize("body", [
    {},                                                                  # missing fields
    {"request_id": "job1-slide-01"},                                     # missing params
    {"request_id": "bad id!", "params": VALID},                          # invalid request_id
    {"request_id": "job1-slide-01", "params": {**VALID, "prompt": ""}},
    {"request_id": "job1-slide-01", "params": {**VALID, "width": 1000}},  # not a multiple of 64
    {"request_id": "job1-slide-01", "params": {**VALID, "width": 1536, "height": 1536}},  # too many pixels
    {"request_id": "job1-slide-01", "params": {**VALID, "steps": 200}},
    {"request_id": "job1-slide-01", "params": {**VALID, "guidance": 50}},
    {"request_id": "job1-slide-01", "params": {**VALID, "steps": "abc"}},
])
def test_invalid_requests_return_422_without_gpu(client, body):
    r = client.post("/v1/generate", json=body)
    assert r.status_code == 422
    assert FakeWorker.spawned == []


def test_result_and_cancel_reject_malformed_call_ids(client):
    assert client.get("/v1/result/not-a-call").status_code == 404
    assert client.post("/v1/cancel/../../etc").status_code == 404
