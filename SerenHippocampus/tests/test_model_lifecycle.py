"""
The small model is on only while the hippocampus needs it.

Runs a REAL process as the "model server": tests/fake_model_server.py, which
answers /health and /v1/chat/completions on a free port. So what is exercised
is the actual start, health wait, keep-warm and stop, not a mock of them.

- a sleep starts the managed model, drafts on it, and the tick stops it once
  it has been idle for keep_warm_seconds
- a server that was already up is borrowed and never stopped
- a model that will not start fails the sleep and KEEPS the brief (no
  mechanical copy of the fragments), and the next attempt waits
  retry_after_seconds
- manage off and the model down: the same honest failure
- the Observatory route: start and stop go to /api/v1/service/<name>/{start,stop}
  with the bearer
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from conftest import short

HERE = Path(__file__).parent
FAKE = HERE / "fake_model_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cmd(port: int) -> str:
    return subprocess.list2cmdline([sys.executable, str(FAKE), str(port)])


def _down(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
        return False
    except Exception:  # noqa: BLE001
        return True


def _brief(memory, promote=None):
    return memory.post("/brief", json={"summary": "what mattered", "promote_hints": promote or [],
                                       "noise_hints": []}).json()["id"]


def _managed(make_hippo, port: int, **lc):
    h = make_hippo()
    h._cfg.model.url = f"http://127.0.0.1:{port}/v1"
    h._cfg.model.name = "fake"
    h._cfg.model.lifecycle.manage = True
    h._cfg.model.lifecycle.start = lc.pop("start", _cmd(port))
    h._cfg.model.lifecycle.ready_timeout_seconds = lc.pop("ready_timeout_seconds", 30)
    h._cfg.model.lifecycle.poll_seconds = 0.2
    for k, v in lc.items():
        setattr(h._cfg.model.lifecycle, k, v)
    # the real model call, not the conftest fake
    h.__dict__.pop("_call_model", None)
    return h


def test_a_sleep_starts_the_model_and_the_tick_stops_it_when_idle(memory, make_hippo):
    port = _free_port()
    h = _managed(make_hippo, port, keep_warm_seconds=0)
    try:
        assert _down(port)
        short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
        _brief(memory, ["nuc"])
        rep = h.check()
        assert rep["status"] == "sleeping" and rep["sleep"]["error"] is None, rep
        assert rep["sleep"]["operations"] >= 1
        ops = memory.get(f"/dockets/{rep['sleep']['docket_id']}").json()["operations"]
        assert all("mechanical fallback" not in (op.get("rationale") or "") for op in ops), "drafted BY the model"
        assert h.model.started_by_us and h.model.state == "up"
        assert [e["event"] for e in h.state["events"]].count("model_started") == 1
        assert h.model.maybe_stop() is True
        assert _down(port), "stopped after keep-warm"
        assert h.model.state == "down" and not h.model.started_by_us
        assert "model_stopped" in [e["event"] for e in h.state["events"]]
    finally:
        h.model.shutdown()


def test_keep_warm_holds_it_up_between_a_sleep_and_a_redraft(memory, make_hippo):
    port = _free_port()
    h = _managed(make_hippo, port, keep_warm_seconds=3600)
    try:
        h.model.ensure_up()
        assert h.model.maybe_stop() is False and not _down(port), "still warm"
        assert h.model.maybe_stop(now=time.time() + 3601) is True and _down(port)
    finally:
        h.model.shutdown()


def test_a_server_already_up_is_borrowed_and_never_stopped(memory, make_hippo):
    port = _free_port()
    theirs = subprocess.Popen([sys.executable, str(FAKE), str(port)])
    try:
        deadline = time.time() + 20
        while _down(port) and time.time() < deadline:
            time.sleep(0.2)
        h = _managed(make_hippo, port, keep_warm_seconds=0)
        h.model.ensure_up()
        assert h.model.state == "up" and not h.model.started_by_us
        assert h.model.maybe_stop(now=time.time() + 99999) is False
        h.model.shutdown()
        assert not _down(port), "someone else's server is left alone"
    finally:
        theirs.terminate(); theirs.wait(timeout=10)


def test_a_model_that_will_not_start_fails_the_sleep_and_keeps_the_brief(memory, make_hippo):
    port = _free_port()
    bad = subprocess.list2cmdline([sys.executable, "-c", "import sys; sys.exit(3)"])
    h = _managed(make_hippo, port, start=bad, ready_timeout_seconds=10, retry_after_seconds=900)
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory, ["t"])
    rep = h.check()
    s = rep["sleep"]
    assert s["error"] and "exited with code 3" in s["error"], s
    assert s["operations"] == 0 and s["docket_id"] is None, "no mechanical copy of the fragments"
    assert [b["id"] for b in memory.get("/brief").json()["entries"]] == [bid], "the brief is kept for the next check"
    assert "model_start_failed" in [e["event"] for e in h.state["events"]]
    t0 = time.time()
    rep2 = h.check()
    assert "next attempt in" in (rep2["sleep"]["error"] or "") and time.time() - t0 < 5, "backs off, no relaunch"


def test_manage_off_and_the_model_down_is_an_honest_failure(memory, make_hippo):
    port = _free_port()
    h = _managed(make_hippo, port)
    h._cfg.model.lifecycle.manage = False
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory, ["t"])
    rep = h.check()
    assert "manage is off" in (rep["sleep"]["error"] or "")
    assert memory.get("/dockets").json()["count"] == 0
    assert [b["id"] for b in memory.get("/brief").json()["entries"]] == [bid]


def test_the_observatory_starts_and_stops_a_registered_service(memory, make_hippo):
    calls: list[tuple[str, str]] = []
    up = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200 if up["on"] else 503)
        if path.startswith("/api/v1/service/seren-llama/"):
            calls.append((path.rsplit("/", 1)[-1], request.headers.get("authorization", "")))
            up["on"] = path.endswith("/start")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    h = make_hippo()
    h._cfg.model.url = "http://model.test/v1"
    lc = h._cfg.model.lifecycle
    lc.manage = True
    lc.observatory_url = "http://obs.test"
    lc.observatory_service = "seren-llama"
    lc.observatory_token = "obs-secret"
    lc.health_url = "http://model.test/health"
    lc.poll_seconds = 0.05
    lc.keep_warm_seconds = 0
    h.model._transport = httpx.MockTransport(handler)
    h.model.ensure_up()
    assert h.model.started_by_us and calls == [("start", "Bearer obs-secret")]
    assert h.model.maybe_stop() is True
    assert calls[-1] == ("stop", "Bearer obs-secret") and up["on"] is False


def test_status_reports_the_model(memory, bridge, hcfg):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    hcfg.model.url = "http://127.0.0.1:9/v1"
    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        ml = tc.get("/status").json()["model_lifecycle"]
        assert ml["managed"] is False and ml["health_url"] == "http://127.0.0.1:9/health"
    client.close()
