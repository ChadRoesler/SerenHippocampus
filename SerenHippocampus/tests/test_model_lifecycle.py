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

from conftest import as_json, short

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
        ops = memory.get(f"/drafts/{rep['sleep']['draft_id']}").json()["operations"]
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
    assert s["operations"] == 0 and s["draft_id"] is None, "no mechanical copy of the fragments"
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
    assert memory.get("/drafts").json()["count"] == 0
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


def test_in_a_cluster_the_model_comes_through_lodestar(memory, make_hippo):
    """Design note: hippocampus => Lodestar => Observatory => start llama
    => the Observatory waits until llama is up => Lodestar tells the
    hippocampus it is ready, and where. One call; the lease is released when
    the hippocampus is done."""
    calls: list[tuple[str, dict, str]] = []
    up = {"on": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "192.0.2.101" and request.url.path == "/health":
            return httpx.Response(200 if up["on"] else 503)
        if request.url.host == "lodestar.test" and request.url.path.startswith("/api/v1/service/llama/"):
            verb = request.url.path.rsplit("/", 1)[-1]
            body = __import__("json").loads(request.content)
            calls.append((verb, body, request.headers.get("authorization", "")))
            if verb == "ensure":
                up["on"] = True
                return httpx.Response(200, json={"ok": True, "service": "llama", "node": "node-a", "ready": True,
                                                 "started": True, "base_url": "http://192.0.2.101:8090",
                                                 "port": 8090, "waited_seconds": 41.0, "holders": [body["holder"]]})
            up["on"] = False
            return httpx.Response(200, json={"ok": True, "service": "llama", "node": "node-a", "stopped": True, "holders": []})
        return httpx.Response(404)

    h = make_hippo()
    h._cfg.model.url = "http://placeholder.invalid/v1"
    lc = h._cfg.model.lifecycle
    lc.lodestar_url = "http://lodestar.test"
    lc.lodestar_token = "lode-secret"
    lc.ready_timeout_seconds = 30
    lc.poll_seconds = 0.05
    lc.keep_warm_seconds = 0
    h.model._transport = httpx.MockTransport(handler)
    assert h.model.managed and h.model.via_lodestar, "naming lodestar_url is the request"

    h.model.ensure_up()
    verb, body, auth = calls[0]
    assert (verb, auth) == ("ensure", "Bearer lode-secret")
    assert body["holder"] == "seren-hippocampus" and body["wait_seconds"] == 30.0
    assert h._cfg.model.url == "http://192.0.2.101:8090/v1", "the answer says where the model is"
    assert h.model.health_url == "http://192.0.2.101:8090/health" and h.model.state == "up"
    h.model.ensure_up()
    assert len(calls) == 1, "holding the lease and the model answering: nothing more to ask"

    assert h.model.maybe_stop() is True
    assert calls[-1][0] == "release" and calls[-1][1]["holder"] == "seren-hippocampus"
    assert h.model.started_by_us is False and h.model.snapshot()["via"] == "lodestar"


def test_lodestar_saying_no_fails_the_start_with_its_reason(memory, make_hippo):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ensure"):
            return httpx.Response(200, json={"ok": False, "service": "llama", "node": "node-a", "ready": False,
                                             "error": "llama did not start: model.gguf: no such file"})
        return httpx.Response(503)

    h = make_hippo()
    h._cfg.model.url = "http://placeholder.invalid/v1"
    lc = h._cfg.model.lifecycle
    lc.lodestar_url = "http://lodestar.test"
    lc.ready_timeout_seconds = 1
    lc.poll_seconds = 0.05
    h.model._transport = httpx.MockTransport(handler)
    from seren_hippocampus.model_lifecycle import ModelUnavailable
    with pytest.raises(ModelUnavailable, match="model.gguf: no such file"):
        h.model.ensure_up()
    assert h.model.started_by_us is False and h.model.state == "failed"


def test_a_model_that_already_answers_is_still_leased_through_lodestar(memory, make_hippo):
    """Someone else has llama up. The hippocampus must still take a lease, or
    that someone's release stops the model in the middle of a sleep."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200)
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "ready": True, "already_running": True, "node": "node-a",
                                         "base_url": "http://model.test:8090", "holders": ["symposium", "seren-hippocampus"]})

    h = make_hippo()
    h._cfg.model.url = "http://model.test:8090/v1"
    h._cfg.model.lifecycle.lodestar_url = "http://lodestar.test"
    h.model._transport = httpx.MockTransport(handler)
    h.model.ensure_up()
    assert calls == ["ensure"] and h.model.started_by_us is True


def test_the_local_ways_are_untouched_by_cluster_mode(memory, make_hippo):
    """A single box keeps starting its own server: a command wins over lodestar_url."""
    h = make_hippo()
    lc = h._cfg.model.lifecycle
    assert h.model.via_lodestar is False and h.model.snapshot()["via"] == "local"
    lc.lodestar_url = "http://lodestar.test"
    lc.manage, lc.start = True, "llama-server -m x.gguf"
    assert h.model.via_lodestar is False, "a local command is the more specific instruction"


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


# ── no silent model failures ─────────────────────────────────────────────────

def test_a_model_that_answers_garbage_fails_the_sleep_and_copies_nothing(memory, make_hippo):
    """The first real draft (25 Sept) was a raw short-term copied into a
    'core' because the draft failed. A configured model that fails now
    proposes nothing, the sleep says why, and the brief is kept."""
    h = make_hippo(model=lambda prompt: "I think the answer is probably something like memory")
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory, ["t"])
    rep = h.check()["sleep"]
    assert rep["draft_id"] is None and rep["operations"] == 0, "no mechanical copy"
    assert rep["error"] and "not the JSON asked for" in rep["error"]
    assert rep["model_failures"] and rep["model_failures"][0]["stage"] == "draft"
    assert "draft_failed" in [e["event"] for e in h.state["events"]]
    assert [b["id"] for b in memory.get("/brief").json()["entries"]] == [bid], "the brief is kept"


def test_a_redraft_that_fails_is_reported_not_swallowed(memory, make_hippo):
    from conftest import review
    calls = {"n": 0}

    def model(prompt: str) -> str:
        calls["n"] += 1
        if "DENIED" in prompt:
            return "sorry, I was thinking"
        return as_json({"operations": [{"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model)
    short(memory, "a", "t"); short(memory, "b", "t")
    _brief(memory, ["t"])
    did = h.check()["sleep"]["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "no"}])
    t = h.tend()
    assert t["resubmitted"] == [] and t["model_failures"] and t["model_failures"][0]["stage"] == "redraft"
    assert "redraft_failed" in [e["event"] for e in h.state["events"]]


def test_thinking_is_switched_off_by_default(memory, make_hippo):
    """The stand-in server thinks (empty answer) unless the request carries
    chat_template_kwargs.enable_thinking=false - the same trap as Qwen3.5 on
    llama.cpp. With the default extra_body the sleep drafts; with it removed
    the sleep fails loudly."""
    port = _free_port()
    h = _managed(make_hippo, port, keep_warm_seconds=0)
    try:
        short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
        _brief(memory, ["nuc"])
        h._cfg.model.extra_body = {}
        rep = h.check()["sleep"]
        assert rep["error"] and rep["draft_id"] is None, "thinking on: no usable answer, and it says so"
        h._cfg.model.extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
        rep = h.check()["sleep"]
        assert rep["error"] is None and rep["operations"] >= 1, rep
    finally:
        h.model.shutdown()


def test_every_draft_says_which_model_wrote_it(memory, make_hippo):
    """Design note: validate swapping consolidators and catch drift. The
    audit can only do that if each draft is stamped with the model that wrote
    it - the name the SERVER gives (the gguf), not only the config's alias -
    and the prompt version. A redraft is stamped too; so is a sleep with no
    model (mechanical)."""
    from conftest import review
    from seren_hippocampus.sleep import PROMPT_VERSION
    port = _free_port()
    h = _managed(make_hippo, port, keep_warm_seconds=0)
    try:
        short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
        _brief(memory, ["nuc"])
        did = h.check()["sleep"]["draft_id"]
        d = memory.get(f"/drafts/{did}").json()
        assert d["extra"]["model_served"] == "fake-2b-Q4_K_M.gguf", d["extra"]
        assert d["extra"]["model_prompt"] == PROMPT_VERSION and d["extra"]["model_mode"] == "model"
        review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: say it plainly"}])
        again = h.tend()["resubmitted"][0]["draft_id"]
        assert memory.get(f"/drafts/{again}").json()["extra"]["model_served"] == "fake-2b-Q4_K_M.gguf"
        audit = memory.get("/audit").json()
        assert audit["models"][0]["model"] == f"fake-2b-Q4_K_M.gguf · prompt {PROMPT_VERSION}"
        assert audit["models"][0]["drafts"] == 2
    finally:
        h.model.shutdown()
    h._cfg.model.url = ""
    assert h._stamp() == {"model_mode": "mechanical", "model_prompt": PROMPT_VERSION}


# ── the easy way: server + model_path (28 Sept 2026) ─────────────────────

def _built(make_hippo, tmp_path, url="http://127.0.0.1:7200/v1", **lc):
    server = tmp_path / "llama-server.exe"; server.write_text("")
    model = tmp_path / "qwen3-4b.gguf"; model.write_text("")
    h = make_hippo()
    h._cfg.model.url = url
    h._cfg.model.lifecycle.server = lc.pop("server", str(server))
    h._cfg.model.lifecycle.model_path = lc.pop("model_path", str(model))
    for k, v in lc.items():
        setattr(h._cfg.model.lifecycle, k, v)
    return h, str(server), str(model)


def test_the_server_and_model_file_build_the_command(make_hippo, tmp_path):
    h, server, model = _built(make_hippo, tmp_path, server_args="-ngl 99 -c 8192 -fa on")
    assert h.model.managed, "naming the server and the model file turns management on"
    assert h.model.built_command() == [server, "-m", model, "--host", "127.0.0.1", "--port", "7200",
                                       "-ngl", "99", "-c", "8192", "-fa", "on"]


def test_host_and_port_come_from_the_model_url(make_hippo, tmp_path):
    h, _, _ = _built(make_hippo, tmp_path, url="http://0.0.0.0:9123/v1", server_args="")
    assert h.model.built_command()[3:] == ["--host", "0.0.0.0", "--port", "9123"]


def test_a_hand_written_start_line_still_wins(make_hippo, tmp_path):
    h, _, _ = _built(make_hippo, tmp_path, start="my-server --port 7200")
    assert not h.model.built and h.model.managed is False, "start alone needs manage: true, as before"
    h._cfg.model.lifecycle.manage = True
    assert h.model.managed


def test_a_missing_model_file_fails_and_names_the_path(memory, make_hippo, tmp_path):
    port = _free_port()
    h, _, _ = _built(make_hippo, tmp_path, url=f"http://127.0.0.1:{port}/v1",
                     model_path=str(tmp_path / "not-here.gguf"), ready_timeout_seconds=1)
    h.__dict__.pop("_call_model", None)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    _brief(memory, ["nuc"])
    rep = h.check()
    err = rep["sleep"]["error"] or ""
    assert "model_path not found" in err and "not-here.gguf" in err, rep
    assert h.model.state == "failed"
