"""
The tend cycle: redrafts on their own setting, apart from the heartbeat
(on a small box a tend that runs whenever it likes can OOM the main model,
especially when the tend pulls the drafting model up).

One timer used to drive everything, and its tend redrafted - which starts the
small model - whenever it happened to land. Pinned here:

- the heartbeat (tick_seconds) and the redraft cadence (tend_interval_seconds,
  600) are separate; an older config that set only the tend interval keeps
  that as its heartbeat
- tend cycle ON: the loop redrafts at most once per tend_interval_seconds; a
  turn in between leaves the denied operations and says so (deferred)
- tend cycle OFF: the loop never redrafts on the timer. It does at sleep time:
  once bedtime has passed, or when a NEW brief is waiting - the open chain's
  own brief does not count
- either way a turn still closes a chain that landed (no model needed), and
  tend() by hand always redrafts
- sleep_status says when redrafts are waiting because the cycle is off
"""
from __future__ import annotations

import pytest

from conftest import as_json, review, short
from seren_hippocampus.config import SleepConfig, load_config


def _model(prompt: str) -> str:
    if "was DENIED" in prompt:
        return as_json({"content": "The NUC stays on focal.", "rationale": "from the critique"})
    return as_json({"operations": [{"kind": "new_core", "content": "The NUC moved to jammy.",
                                    "rationale": "said twice", "source_indexes": [0, 1]}]})


def _denied_chain(memory, h) -> str:
    """A sleep's draft with its one operation denied - a redraft is owed."""
    short(memory, "a", "t"); short(memory, "b", "t")
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: it did not move"}])
    return did


def test_heartbeat_and_tend_interval_are_separate():
    s = SleepConfig()
    assert (s.tick_seconds, s.tend_cycle, s.tend_interval_seconds) == (300, True, 600)
    assert s.heartbeat_seconds() == 300
    assert SleepConfig(tend_interval_seconds=120).heartbeat_seconds() == 120, "an older config: it WAS the heartbeat"
    assert SleepConfig(tend_cycle=False, tend_interval_seconds=120).heartbeat_seconds() == 300
    assert SleepConfig(tick_seconds=1).tick_seconds == 30 and SleepConfig(tend_interval_seconds=5).tend_interval_seconds == 60


def test_the_yaml_turns_the_cycle_off(tmp_path):
    p = tmp_path / "seren-hippocampus.yaml"
    p.write_text("sleep:\n  tend_cycle: false\n  tend_interval_seconds: 900\n", encoding="utf-8")
    s = load_config(str(p)).sleep
    assert s.tend_cycle is False and s.tend_interval_seconds == 900


def test_on_the_loop_redrafts_once_per_interval(make_hippo, memory):
    h = make_hippo(model=_model)
    did = _denied_chain(memory, h)
    first = h.tick()["tend"]
    assert len(first["resubmitted"]) == 1 and first["deferred"] == []
    review(memory, first["resubmitted"][0]["draft_id"], [{"op": 0, "verdict": "deny", "critique": "DENIED: still no"}])
    between = h.tick()["tend"]
    assert between["resubmitted"] == [] and between["deferred"] == [did], "inside the interval: it waits, and says so"
    h._next_redraft_at = 0.0                                # the interval comes round
    assert len(h.tick()["tend"]["resubmitted"]) == 1


def test_off_the_loop_does_not_redraft_until_sleep_time(make_hippo, memory):
    h = make_hippo(model=_model, tend_cycle=False)
    did = _denied_chain(memory, h)
    t = h.tick()["tend"]
    assert t["resubmitted"] == [] and t["deferred"] == [did]
    assert h._open_chain(), "the chain stays open, its denied operation waiting"
    assert h.redraft_due() is False


def test_off_bedtime_is_sleep_time(make_hippo, memory):
    h = make_hippo(model=_model, tend_cycle=False)
    _denied_chain(memory, h)
    assert h.redraft_due(now=h.next_sleep_due() + 1) is True
    h.next_sleep_due = lambda now=None: 0.0                 # type: ignore[method-assign]  # bedtime has passed
    assert len(h.tick()["tend"]["resubmitted"]) == 1


def test_off_a_new_brief_is_sleep_time_but_the_chains_own_is_not(make_hippo, memory):
    h = make_hippo(model=_model, tend_cycle=False)
    short(memory, "a", "t"); short(memory, "b", "t")
    own = memory.post("/brief", json={"summary": "tonight", "promote_hints": [], "noise_hints": []}).json()["id"]
    did = h.sleep()["draft_id"]
    assert memory.get(f"/drafts/{did}").json()["brief_id_used"] == own
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: it did not move"}])
    assert h._new_brief_waiting() is False, "the open chain's own brief is still open, and is not a new one"
    assert h.tick()["tend"]["deferred"] == [did]
    memory.post("/brief", json={"summary": "the next night", "promote_hints": [], "noise_hints": []})
    assert h._new_brief_waiting() is True
    assert len(h.tick()["tend"]["resubmitted"]) == 1


def test_off_a_landed_chain_still_closes_and_by_hand_always_redrafts(make_hippo, memory):
    h = make_hippo(model=_model, tend_cycle=False)
    short(memory, "a", "t"); short(memory, "b", "t")
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "approve"}])
    t = h.tick()["tend"]
    assert t["closed"] == [did] and t["deferred"] == [], "closing needs no model"
    h2 = make_hippo(model=_model, tend_cycle=False)
    _denied_chain(memory, h2)
    assert h2.tick()["tend"]["resubmitted"] == []
    assert len(h2.tend()["resubmitted"]) == 1, "tend_now: someone asked"


async def test_status_says_redrafts_are_waiting(make_hippo, memory):
    pytest.importorskip("mcp")
    from seren_hippocampus.mcp.tools import HippocampusToolImpl
    h = make_hippo(model=_model, tend_cycle=False)
    _denied_chain(memory, h)
    h.tick()
    s = await HippocampusToolImpl(h, h._mem, h._cfg).sleep_status()
    assert s["tend_cycle"] is False and s["last_tend"]["deferred"] == 1
    assert "tend cycle is off" in s["say"] and "tend_now" in s["say"]
    on = make_hippo(model=_model)
    assert "tend cycle" not in (await HippocampusToolImpl(on, on._mem, on._cfg).sleep_status())["say"]
