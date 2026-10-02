"""
The nudge and the hand-over: the two wires the cycle was missing.

THE NUDGE. The hippocampus could wake the main model (the ripple); the model
could only wait for the hippocampus's timer. On 1 Oct 2026 a chain with five
minutes of work in it took twenty-five. Now the model's last act is 'your
turn', and the hippocampus carries on at once. the user: "for you, its gotta be
direct neuron to neuron."

THE HAND-OVER. the user's Nano floor, the same day: the hippocampus asks for a
brief, the main model writes it and its server is shut down, the hippocampus is
poked and drafts, and "when the draft is ready, pokes observ to poke you, and
shuts down so that the main model can run. Its the prevention of OOMing on
limited resources." The hippocampus's half: stop its own model BEFORE it pokes.

Pinned here:
- a nudge lets the next tick redraft whatever the clock or the tend-cycle
  setting says, once; it wakes the loop when there is one
- a nudged turn waits for the woken session to leave, and never kills it
- a ripple that was skipped (the last one still running) is fired again
- the tick stops the model at once when no chain is open; mid-chain it stays
  warm - unless handover is on, when it goes before the poke
- every default message ends by asking for the nudge
"""
from __future__ import annotations

import subprocess
import sys
import time

from fastapi.testclient import TestClient

from conftest import as_json, review, short

from seren_hippocampus.ripple import NUDGE


def _denied_chain(memory, h) -> str:
    a = short(memory, "the model runs on 7200", "t")
    did = memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "x", "topic": "t", "source_short_ids": [a]}],
        "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "say what it is"}])
    return did


# ── the nudge ─────────────────────────────────────────────────────────────────
def test_a_nudge_redrafts_now_whatever_the_clock_says(memory, make_hippo):
    """Tend cycle off and not bedtime: redrafts wait - until 'your turn'."""
    h = make_hippo(model=lambda p: as_json({"content": "The model runs on port 7200."}), tend_cycle=False)
    h.state["last_sleep"] = {"finished_at": time.time()}          # just slept: bedtime is far off
    did = _denied_chain(memory, h)
    t = h.tick()["tend"]
    assert t["resubmitted"] == [] and t["deferred"] == [did], "no nudge: it waits for sleep time"

    r = h.nudge()
    assert r == {"ok": True, "woke": False, "message": "noted; the next tick carries on"}
    assert h.state["events"][-1]["event"] == "nudged"
    t = h.tick()["tend"]
    assert len(t["resubmitted"]) == 1 and t["deferred"] == []
    assert h.redraft_due() is False, "one nudge, one turn"


def test_a_nudge_beats_the_tend_interval(memory, make_hippo):
    h = make_hippo(tend_cycle=True, tend_interval_seconds=600)
    assert h.redraft_due() is True and h.redraft_due() is False    # the interval has just started
    h.nudge()
    assert h.redraft_due() is True and h.redraft_due() is False


def test_a_nudge_wakes_the_loop_when_there_is_one(make_hippo):
    h = make_hippo()
    woken: list[int] = []
    h._wake = lambda: woken.append(1)
    assert h.nudge()["woke"] is True and woken == [1]
    h._wake = lambda: 1 / 0                                      # a loop that cannot be woken: the tick still honours it
    assert h.nudge()["woke"] is False and h._nudged is True


def test_the_route_runs_a_turn_and_the_brief_is_slept_on(memory, bridge, hcfg):
    """external mode has no loop: the nudge runs one turn itself."""
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient
    app = create_app(hcfg, memory_client=MemoryClient("http://memory.test", transport=bridge))
    with TestClient(app) as tc:
        short(memory, "a", "t"); short(memory, "b", "t")
        assert tc.post("/sleep").json()["asked_for_brief"] is True       # sleep starts > asks for a brief
        memory.post("/brief", json={"summary": "s", "promote_hints": ["t"], "noise_hints": []})
        r = tc.post("/nudge")                                            # 'the brief is there'
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["woke"] is True
        end = time.time() + 20
        while time.time() < end and not memory.get("/drafts", params={"status": "pending"}).json()["count"]:
            time.sleep(0.1)
        assert memory.get("/drafts", params={"status": "pending"}).json()["count"] == 1, "it slept on the brief"


def test_the_tool_is_there_and_returns_at_once(make_hippo):
    import asyncio
    import pytest
    pytest.importorskip("mcp")
    from seren_hippocampus.mcp.tools import TOOL_NAMES, HippocampusToolImpl
    h = make_hippo()
    assert "nudge" in TOOL_NAMES
    assert asyncio.run(HippocampusToolImpl(h, h._mem, h._cfg).nudge())["ok"] is True and h._nudged is True


# ── the session that nudged is still in the room ──────────────────────────────
def test_settle_waits_for_a_woken_session_and_never_kills_it(make_hippo):
    h = make_hippo()
    assert h.ripple.settle(timeout=0.0) is True, "nobody is in the room"
    quick = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    h.ripple._runner._running["tend_resubmitted"] = quick
    t0 = time.time()
    assert h.ripple.settle(timeout=10, poll=0.05) is True and time.time() - t0 >= 0.4
    slow = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    h.ripple._runner._running["tend_resubmitted"] = slow
    try:
        assert h.ripple.settle(timeout=0.3, poll=0.05) is False
        assert slow.poll() is None, "a slow reviewer is waited for, not killed"
    finally:
        slow.kill(); slow.wait()


def test_a_skipped_ripple_is_fired_again_on_the_next_tick(memory, make_hippo):
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", ["x"]
    answers = [{"type": "script", "ok": False, "skipped": "the last ripple for this event is still running"},
               {"type": "script", "ok": True, "pid": 1}]
    fired: list[str] = []

    def fire(ev):
        fired.append(ev["event"])
        return answers.pop(0)
    h.ripple.fire = fire                                          # type: ignore[method-assign]
    h._emit("tend_resubmitted", draft_id="d" * 32, terminal=True)
    assert h._ripple_retry is not None and fired == ["tend_resubmitted"]
    h.tick()
    assert fired == ["tend_resubmitted", "tend_resubmitted"] and h._ripple_retry is None
    assert h.state["events"][-1]["event"] != "tend_resubmitted" or h.state["events"][-1]["ripple"]["ok"]


# ── stop as needed ────────────────────────────────────────────────────────────
class _Model:
    """The lifecycle's stop calls, recorded."""
    def __init__(self, order):
        self.started_by_us, self.order = True, order

    def stop_now(self, why="not needed"):
        self.order.append(f"stop:{why}")
        self.started_by_us = False
        return True

    def maybe_stop(self, now=None):
        self.order.append("maybe_stop")
        return False


def test_the_tick_stops_the_model_as_soon_as_the_cycle_is_over(memory, make_hippo):
    order: list[str] = []
    h = make_hippo()
    h.model = _Model(order)                                       # type: ignore[assignment]
    h.tick()
    assert order == ["stop:the cycle is over"], "no chain open: nothing will call it before the next sleep"


def test_mid_chain_it_stays_warm_for_the_redraft(memory, make_hippo):
    order: list[str] = []
    h = make_hippo()
    a = short(memory, "a", "t")
    memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": "x", "topic": "t",
                                                  "source_short_ids": [a]}]})          # waits for review
    h.model = _Model(order)                                       # type: ignore[assignment]
    h.tick()
    assert order == ["maybe_stop"], "a chain is open: keep_warm_seconds decides"


def test_handover_stops_the_small_model_before_the_main_one_is_poked(make_hippo):
    order: list[str] = []
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", ["x"]
    h.model = _Model(order)                                       # type: ignore[assignment]
    h.ripple.fire = lambda ev: order.append(f"poke:{ev['event']}") or {"ok": True}   # type: ignore[method-assign]

    h._emit("draft_submitted", draft_id="d" * 32)
    assert order == ["poke:draft_submitted"], "handover off: it stays warm"

    order.clear()
    h._cfg.model.lifecycle.handover = True
    h._emit("draft_submitted", draft_id="d" * 32)
    h.model.started_by_us = True
    h._emit("tend_resubmitted", draft_id="d" * 32)
    assert order == ["stop:handing over to the main model", "poke:draft_submitted",
                     "stop:handing over to the main model", "poke:tend_resubmitted"]

    order.clear()
    h._emit("brief_requested", due_at=1.0)
    assert order == ["poke:brief_requested"], "asking for a brief hands nothing over: the small model is not up"


def test_stop_now_only_stops_what_this_hippocampus_started(make_hippo):
    h = make_hippo()
    assert h.model.stop_now() is False, "nothing managed, nothing started: nothing stopped"
    assert not [e for e in h.state.get("events") or [] if e["event"] == "model_stopped"]


# ── the wire back is asked for ────────────────────────────────────────────────
def test_every_default_message_ends_by_asking_for_the_nudge(make_hippo):
    h = make_hippo()
    for ev in ({"event": "brief_requested"}, {"event": "draft_submitted", "draft_id": "x"},
               {"event": "tend_resubmitted", "draft_id": "x"},
               {"event": "tend_resubmitted", "draft_id": "x", "terminal": True}, {"event": "brief_unmatched"}):
        assert h.ripple.message(ev).endswith(NUDGE), ev
    h._cfg.ripple.messages = {"draft_submitted": "mine"}
    assert h.ripple.message({"event": "draft_submitted"}) == "mine", "an operator's wording is left as written"
