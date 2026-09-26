"""
Replay: the same inputs, another model, side by side.

Chad, 26 Sept 2026: "a replay, so that we can validate when one seems
better." Pinned here: every draft and redraft saves its model calls; a replay
sends those prompts to a candidate (a REAL server process on a free port),
parses and validates its answers the way a sleep does, and returns both sides
with the verdicts, what landed and plain checks - and never submits anything
to Memory. A candidate that is down is a result, not a crash.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import as_json, review, short
from seren_hippocampus import replay as rp

FAKE = Path(__file__).parent / "fake_model_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def candidate():
    """A second model: the stand-in server, answering 'The NUC stays on focal.'"""
    port = _free_port()
    p = subprocess.Popen([sys.executable, str(FAKE), str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/v1"
    p.terminate()
    p.wait(timeout=10)


def _two_step_model(prompt: str) -> str:
    if "DENIED" in prompt:
        return as_json({"content": "The NUC stays on focal; jammy broke it.", "rationale": "from the critique"})
    return as_json({"operations": [{"kind": "new_core", "content": "The NUC moved to jammy on 2026-09-23.",
                                    "rationale": "said twice", "source_indexes": [0, 1]}]})


def _seed(memory, make_hippo):
    h = make_hippo(model=_two_step_model)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    memory.post("/brief", json={"summary": "nuc talk", "promote_hints": ["nuc"], "noise_hints": []})
    did = h.check()["sleep"]["draft_id"]
    return h, did


def test_every_draft_and_redraft_saves_its_model_calls(memory, make_hippo):
    h, did = _seed(memory, make_hippo)
    pk = rp.load_packet(h._state_path(), did)
    assert pk and pk["attempt"] == 1 and len(pk["calls"]) == 1
    call = pk["calls"][0]
    assert call["stage"] == "draft" and "Fragments:" in call["prompt"]
    assert {e["content"] for e in call["entries"]} == {"the nuc stays on focal", "the nuc hates jammy"}
    assert call["ops"][0]["content"].startswith("The NUC moved to jammy"), "what came of it is kept"
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: no date in the fragments"}])
    again = h.tend()["resubmitted"][0]["draft_id"]
    pk2 = rp.load_packet(h._state_path(), again)
    assert pk2["attempt"] == 2 and pk2["redraft_of"] == did
    assert pk2["calls"][0]["stage"] == "redraft" and "no date in the fragments" in pk2["calls"][0]["prompt"]
    assert [p["draft_id"] for p in rp.list_packets(h._state_path())][:2] == [again, did]


def test_a_replay_puts_a_candidate_beside_the_original_and_submits_nothing(memory, make_hippo, candidate):
    h, did = _seed(memory, make_hippo)
    review(memory, did, [{"op": 0, "verdict": "approve"}])
    before = memory.get("/drafts", params={"limit": 100}).json()["count"]

    r = h.replay(did, url=candidate)
    assert memory.get("/drafts", params={"limit": 100}).json()["count"] == before, "a replay never submits"
    assert r["candidate"]["served"] == "fake-2b-Q4_K_M.gguf"
    assert r["candidate"]["calls"][0]["ops"][0]["content"] == "The NUC stays on focal."
    assert r["original"]["reviewed_ops"][0]["status"] == "approved", "the verdict the original got"
    assert r["landed"] == ["The NUC moved to jammy on 2026-09-23."]
    oc, cc = r["original"]["checks"], r["candidate"]["checks"]
    assert oc["invented_numbers"] == ["2026-09-23"], "a date none of the fragments has"
    assert cc["invented_numbers"] == [] and cc["valid"] == 1 and cc["failed_calls"] == 0
    assert oc["overlap_landed"] == 1.0 and 0 < cc["overlap_landed"] < 1


def test_a_candidate_that_is_down_is_a_result(memory, make_hippo):
    h, did = _seed(memory, make_hippo)
    r = h.replay(did, url=f"http://127.0.0.1:{_free_port()}/v1", timeout_seconds=2)
    assert r["candidate"]["checks"]["failed_calls"] == 1 and "stopped answering" in r["candidate"]["calls"][0]["error"]
    with pytest.raises(KeyError):
        h.replay("no-such-draft")


def test_the_routes(memory, bridge, hcfg, candidate):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    client = MemoryClient("http://memory.test", transport=bridge)
    hcfg.model.url = "http://model.test/v1"          # a model is configured; its calls are faked below
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        h = app.state.hippocampus
        h._call_model = lambda prompt, max_tokens=None: _two_step_model(prompt)
        short(memory, "a", "t"); short(memory, "b", "t")
        did = tc.post("/sleep").json()["draft_id"]
        assert did and tc.get("/replays").json()["entries"][0]["draft_id"] == did
        r = tc.post("/replay", json={"draft_id": did, "url": candidate})
        assert r.status_code == 200 and r.json()["candidate"]["checks"]["valid"] == 1
        assert tc.post("/replay", json={"draft_id": "nope"}).status_code == 404
        assert tc.post("/replay", json={}).status_code == 400
        assert "Replay" in tc.get("/viewer").text
    client.close()
