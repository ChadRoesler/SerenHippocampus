"""
The hippocampus is tested against the REAL SerenMemory app, in-process.

Memory runs under starlette's TestClient (so its lifespan builds the store);
an httpx MockTransport bridges the hippocampus's MemoryClient to it. What
these tests exercise is the contract between the two services, not a mock
of it. The small model is the one thing faked: `fake_model` answers with
canned JSON keyed on what the prompt asks for.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from fastapi.testclient import TestClient

from seren_memory.app import create_app as create_memory_app
from seren_memory.config import ConsolidatorConfig, MemoryConfig, StorageConfig

from seren_hippocampus.config import HippocampusConfig, ModelConfig, SleepConfig
from seren_hippocampus.memory_client import MemoryClient
from seren_hippocampus.sleep import Hippocampus


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    try:
        from seren_meninges.updates import UpdateChecker
    except ImportError:
        return

    async def _no_network(self, distribution):
        raise ConnectionError("network disabled in tests")
    monkeypatch.setattr(UpdateChecker, "_fetch_from_index", _no_network)


class BagOfWords(EmbeddingFunction):
    """Deterministic, offline. Similar text -> similar vector, enough for
    the candidate search to find the core the fragments are about."""
    _DIM = 64

    def __init__(self) -> None:
        pass

    @classmethod
    def name(cls) -> str:
        return "bow-hippocampus-test"

    def get_config(self) -> dict:
        return {"dim": self._DIM}

    @classmethod
    def build_from_config(cls, config: dict) -> "BagOfWords":
        return cls()

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            vec = [0.0] * self._DIM
            for tok in text.lower().split():
                vec[hash(tok) % self._DIM] += 1.0
            mag = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / mag for v in vec])
        return out


@pytest.fixture
def memory(tmp_path):
    """The real SerenMemory app, consolidator off, pruned window on."""
    cfg = MemoryConfig(storage=StorageConfig(persist_dir=str(tmp_path / "memory")),
                       consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1))
    app = create_memory_app(cfg, embedding_function=BagOfWords(), _allow_store_reset=True)
    with TestClient(app) as tc:
        yield tc
    try:
        app.state.store.close()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def bridge(memory) -> httpx.MockTransport:
    """Route the hippocampus's HTTP calls into Memory's TestClient."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        r = memory.request(request.method, url, content=request.content,
                           headers={k: v for k, v in request.headers.items()
                                    if k.lower() in ("content-type", "authorization")})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})
    return httpx.MockTransport(handler)


@pytest.fixture
def hcfg(tmp_path) -> HippocampusConfig:
    return HippocampusConfig(
        model=ModelConfig(url=""),                       # mechanical unless a test turns the model on
        sleep=SleepConfig(mode="external", promote_min_evidence=2, max_attempts=3,
                          state_path=str(tmp_path / "state.json")))


@pytest.fixture
def make_hippo(bridge, hcfg) -> Callable[..., Hippocampus]:
    """Build a Hippocampus over the bridged Memory. Pass model=<fn> to give
    it a fake small model (prompt -> text); the model url is then set so
    model_configured reads true."""
    built: list[MemoryClient] = []

    def _make(model=None, **sleep_overrides) -> Hippocampus:
        cfg = hcfg.model_copy(deep=True)
        for k, v in sleep_overrides.items():
            setattr(cfg.sleep, k, v)
        if model is not None:
            cfg.model.url = "http://fake-model"
        client = MemoryClient("http://memory.test", transport=bridge)
        built.append(client)
        h = Hippocampus(cfg, client, log=lambda m: None)
        if model is not None:
            h._call_model = lambda prompt, max_tokens=None: model(prompt)  # type: ignore[method-assign]
        return h
    yield _make
    for c in built:
        c.close()


def short(memory: TestClient, content: str, topic: str = "t", **extra) -> str:
    body = {"content": content, "topic": topic, **extra}
    r = memory.post("/short", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def review(memory: TestClient, docket_id: str, decisions: list[dict]) -> dict:
    r = memory.post(f"/dockets/{docket_id}/review", json={"decisions": decisions})
    assert r.status_code == 200, r.text
    return r.json()


def cores(memory: TestClient, **params) -> dict[str, dict]:
    return {e["id"]: e for e in memory.get("/long", params=params).json()["entries"]}


def as_json(obj) -> str:
    return json.dumps(obj)
