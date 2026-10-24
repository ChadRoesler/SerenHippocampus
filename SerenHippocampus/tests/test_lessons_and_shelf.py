"""
Lessons and the model shelf: the two halves of training the small model.

Lessons (seren_hippocampus.lessons): a reviewed sleep becomes an example - the
prompt the model saw, and what landed as the answer. What is not decided yet
is not a lesson.

The shelf (seren_hippocampus.shelf): a model the Lyceum trained is fetched by
the model box from a Lyceum link, through Lodestar, and switched to under the
sleep's lock. Nothing big travels through the hippocampus.
"""
from __future__ import annotations

import json

import httpx
import pytest

from conftest import as_json, review, short
from seren_hippocampus.lessons import consolidator_set, to_jsonl
from seren_hippocampus.shelf import Shelf, ShelfError
from seren_hippocampus.sleep import Busy


def _model(prompt: str) -> str:
    return as_json({"operations": [{"kind": "new_core", "content": "The NUC runs focal, not jammy.",
                                    "rationale": "said twice", "source_indexes": [0, 1]}]})


def _slept(memory, make_hippo):
    h = make_hippo(model=_model)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    memory.post("/brief", json={"summary": "nuc talk", "promote_hints": ["nuc"], "noise_hints": []})
    did = h.check()["sleep"]["draft_id"]
    return h, did


def test_a_reviewed_sleep_is_a_lesson_with_what_landed(memory, make_hippo):
    h, did = _slept(memory, make_hippo)
    rows, report = consolidator_set(h, h._mem)
    assert rows == [] and "undecided" in json.dumps(report["skipped"]), "not decided yet: not a lesson"

    review(memory, did, [{"op": 0, "verdict": "approve"}])
    rows, report = consolidator_set(h, h._mem)
    assert report["examples"] == 1 and report["operations_by_kind"] == {"new_core": 1}
    ask, answer = rows[0]["messages"]
    assert ask["role"] == "user" and "the nuc stays on focal" in ask["content"]
    ops = json.loads(answer["content"])["operations"]
    assert ops == [{"kind": "new_core", "aging": [0, 1], "content": "The NUC runs focal, not jammy.",
                    "rationale": "No core covers this and it is worth keeping."}], "what landed is the answer"
    assert to_jsonl(rows).count("\n") == 1


# -- the shelf -------------------------------------------------------------------

class Remote:
    """A Lyceum and a Lodestar, as one MockTransport."""

    def __init__(self, running=False):
        self.calls, self.running, self.serving = [], running, "/m/Qwen3-4B-Q5_K_M.gguf"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.host, req.url.path, body))
        p = req.url.path
        if req.url.host == "spark":
            if p == "/api/jobs":
                return httpx.Response(200, json={"jobs": [
                    {"id": "j2", "name": "consolidator v2", "status": {"state": "running"}},
                    {"id": "j1", "name": "consolidator v1", "base_model": "Qwen/Qwen3-4B", "dataset": "lessons",
                     "status": {"state": "done", "eval_loss": 0.107}}]})
            if p == "/api/jobs/j1":
                return httpx.Response(200, json={"spec": {"export": {"quant": "Q5_K_M"}}, "artifacts": [
                    {"path": "adapter/adapter_model.safetensors", "bytes": 10},
                    {"path": "consolidator-v1-Q5_K_M.gguf", "bytes": 2_900_000_000}]})
            if p == "/api/link":
                assert req.headers.get("authorization") == "Bearer ly-tok"
                return httpx.Response(200, json={"url": f"/viewer/dl/CODE/{body['path']}"})
        if req.url.host == "lodestar":
            assert req.headers.get("authorization") == "Bearer lode-tok"
            if p.endswith("/models/fetch") and req.method == "POST":
                return httpx.Response(200, json={"node": "node-a", "ok": True, "fetch": {"state": "fetching"}})
            if p.endswith("/models") and req.method == "GET":
                # Lodestar's /models nests the node's answer under "models", as it really does
                return httpx.Response(200, json={"node": "node-a", "models": {
                    "models": [{"name": "Qwen3-4B-Q5_K_M.gguf", "size_mb": 2755, "serving": True}],
                    "serving": {"current": self.serving, "previous": ""}}})
            if p.endswith("/model") and req.method == "PUT":
                was, self.serving = self.serving, "/m/" + body["name"]
                return httpx.Response(200, json={"node": "node-a", "serving": {"current": self.serving, "previous": was}})
            if p.endswith("/status"):
                return httpx.Response(200, json={"node": "node-a", "status": {"running": self.running}})
            if p.endswith("/restart"):
                return httpx.Response(200, json={"node": "node-a", "result": {"ok": True}})
            if p.endswith("/models/nope.gguf"):
                return httpx.Response(404, json={"node": "node-a", "detail": "no nope.gguf here"})
        return httpx.Response(404, json={"detail": f"nothing at {p}"})


@pytest.fixture
def shelf(make_hippo):
    def _make(remote):
        h = make_hippo()
        h._cfg.lyceum.url, h._cfg.lyceum.bearer_token = "http://spark:7258", "ly-tok"
        lc = h._cfg.model.lifecycle
        lc.lodestar_url, lc.lodestar_token = "http://lodestar:6361", "lode-tok"
        return h, Shelf(h._cfg, h, transport=httpx.MockTransport(remote))
    return _make


def test_a_lyceum_model_is_fetched_by_the_box_from_a_link(shelf):
    remote = Remote()
    h, sh = shelf(remote)
    box = sh.serving()
    assert box["node"] == "node-a" and box["serving"]["current"].endswith("Qwen3-4B-Q5_K_M.gguf")
    assert [m["name"] for m in box["models"]] == ["Qwen3-4B-Q5_K_M.gguf"]
    got = sh.lyceum_models()
    assert [(m["job_id"], m["file"], m["quant"]) for m in got] == [("j1", "consolidator-v1-Q5_K_M.gguf", "Q5_K_M")]
    r = sh.fetch("j1")
    assert r["node"] == "node-a"
    fetch = [c for c in remote.calls if c[2].endswith("/models/fetch")][0]
    assert fetch[3] == {"url": "http://spark:7258/viewer/dl/CODE/consolidator-v1-Q5_K_M.gguf",
                        "name": "consolidator-v1-Q5_K_M.gguf"}, "the box gets a link it can reach, not the file"
    assert any(e["event"] == "model_fetch_started" for e in h.state["events"])


def test_switching_restarts_a_running_model_and_says_what_it_was(shelf):
    remote = Remote(running=True)
    h, sh = shelf(remote)
    r = sh.use("consolidator-v1-Q5_K_M.gguf")
    assert r["restarted"] is True and "is the small model now" in r["say"]
    assert remote.serving == "/m/consolidator-v1-Q5_K_M.gguf"
    ev = [e for e in h.state["events"] if e["event"] == "model_switched"][-1]
    assert ev["name"] == "consolidator-v1-Q5_K_M.gguf" and ev["previous"] == "Qwen3-4B-Q5_K_M.gguf"


def test_a_stopped_model_is_left_for_the_next_sleep(shelf):
    remote = Remote(running=False)
    _, sh = shelf(remote)
    r = sh.use("consolidator-v1-Q5_K_M.gguf")
    assert r["restarted"] is False and not any(c[2].endswith("/restart") for c in remote.calls)


def test_never_under_a_running_sleep(shelf):
    remote = Remote()
    h, sh = shelf(remote)
    h._lock.acquire()
    try:
        with pytest.raises(Busy):
            sh.use("consolidator-v1-Q5_K_M.gguf")
    finally:
        h._lock.release()
    assert not any(c[0] == "PUT" for c in remote.calls)


def test_refusals_carry_their_reason(shelf, make_hippo):
    _, sh = shelf(Remote())
    with pytest.raises(ShelfError) as e:
        sh.forget("nope.gguf")
    assert e.value.status == 404 and "no nope.gguf" in str(e.value)
    bare = Shelf(make_hippo()._cfg, make_hippo())
    with pytest.raises(ShelfError) as e:
        bare.lyceum_models()
    assert "lyceum.url" in str(e.value)
    with pytest.raises(ShelfError) as e:
        bare.serving()
    assert "Lodestar" in str(e.value)
