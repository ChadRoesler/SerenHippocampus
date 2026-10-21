"""
A promote hint lowers the bar to hint_min_evidence, not always to one
(21 Oct 2026). Measured on one persona store that night: 80 of 132 drafted cores
carried one episode's evidence and only 30 cleared promote_min_evidence 3,
because a matched hint dropped the bar to one and the main model writes the
brief. The rule: promotion from short to long is earned, not given. Pinned here:

- the default is still one (the README's one-off joke into an inside joke)
- at hint_min_evidence 2 a hinted lone memory waits; a second fragment on
  the topic clears it; a pin still promotes one
- the drafting worker is told a core is the retelling, never a fragment's
  own words, and that one memory's fragments are one core
"""
from __future__ import annotations

import json

TOPIC = "cabin"
HINT = "the cabin blackout"


def _short(memory, content, topic=TOPIC, **extra):
    r = memory.post("/short", json={"content": content, "topic": topic, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _brief(memory, promote):
    return memory.post("/brief", json={"summary": "what mattered", "promote_hints": list(promote)}).json()["id"]


def test_by_default_a_hint_still_keeps_a_lone_memory(make_hippo, memory):
    _short(memory, "The cabin blackout: cards by headlamp while the rain came down.")
    _brief(memory, [HINT])
    rep = make_hippo().sleep()
    assert rep["operations"] == 1 and rep["hints"]["promote"][HINT] == [TOPIC]


def test_at_two_a_hinted_lone_memory_waits_for_a_second_fragment(make_hippo, memory):
    _short(memory, "The cabin blackout: cards by headlamp while the rain came down.")
    _brief(memory, [HINT])
    rep = make_hippo(hint_min_evidence=2).sleep()
    assert rep["operations"] == 0 and rep["draft_id"] is None, "hinted, matched, and still under the bar"
    assert rep["hints"]["promote"][HINT] == [TOPIC], "the hint did match: this is the bar, not a miss"
    _short(memory, "The cabin blackout again: elderflower cordial became the house drink.")
    _brief(memory, [HINT])
    rep = make_hippo(hint_min_evidence=2).sleep()
    assert rep["operations"] == 1, "two fragments on a hinted topic clear a bar of two"


def test_a_pin_still_promotes_one(make_hippo, memory):
    _short(memory, "The cabin blackout: cards by headlamp while the rain came down.", pinned=True)
    rep = make_hippo(hint_min_evidence=2).sleep()
    assert rep["operations"] == 1


def test_the_worker_is_told_a_core_is_the_retelling(make_hippo, memory):
    _short(memory, "The cabin blackout: cards by headlamp while the rain came down.")
    _short(memory, "The cabin blackout again: elderflower cordial became the house drink.")
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return json.dumps({"operations": [{"kind": "new_core", "content": "The cabin blackout.",
                                           "source_indexes": [0, 1], "rationale": "one night"}]})

    rep = make_hippo(model=model).sleep()
    assert rep["operations"] == 1
    p = prompts[0]
    assert "the retelling" in p and "never a fragment's own words" in p and "kept under the core" in p
    assert "ONE new_core citing all of them" in p
