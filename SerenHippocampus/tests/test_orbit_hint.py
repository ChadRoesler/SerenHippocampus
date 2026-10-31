"""
The orbit is the first candidate (Memory's low core orbit, 30 Oct 2026).

A memory written close to a core carries that core on its metadata
(orbit, orbit_distances). The drafting worker is shown those cores first,
whatever the searches find, and told which memory orbits which, so an
attach lands where the store already put the memory. Pinned here:

- a core a memory orbits is a candidate even when no search returns it,
  shown first, with the memories that orbit it and the distance
- the prompt marks the memory as orbiting, marks the core, and carries
  the orbit rule; with no orbit it says nothing about orbits
- what a replay keeps of each memory includes the orbit
- a Memory that writes orbits puts them on the aging list the sleep reads
  (skipped on a Memory from before orbits)
"""
from __future__ import annotations

import pytest
from conftest import review

from seren_hippocampus.sleep import PROMPT_VERSION


def _core(memory, content: str, topic: str = "games") -> str:
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": content, "topic": topic}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def test_an_orbited_core_is_the_first_candidate_even_when_search_finds_nothing(memory, make_hippo):
    h = make_hippo()
    core = _core(memory, "The user makes push-your-luck board games about flooding harbours.")
    other = _core(memory, "The dentist is on Elm Street.", "errands")
    h._mem.search_long = lambda q, n=5: [{"id": other, "tier": "long", "content": "The dentist is on Elm Street.",
                                          "metadata": {"kind": "core"}}]
    entries = [{"id": "s1", "content": "Designing a new harbour game tonight.",
                "metadata": {"topic": "games", "orbit": [core], "orbit_distances": {core: 0.31}}},
               {"id": "s2", "content": "Also a thought about dice.", "metadata": {"topic": "games"}}]
    got = h._candidates(entries)
    assert [c["id"] for c in got][:2] == [core, other], got
    assert got[0]["orbit_of"] == [0] and got[0]["orbit_distance"] == 0.31
    assert "orbit_of" not in got[1]


def test_the_prompt_says_who_orbits_what(memory, make_hippo):
    h = make_hippo()
    core = _core(memory, "The user makes push-your-luck board games about flooding harbours.")
    h._mem.search_long = lambda q, n=5: []
    entries = [{"id": "s1", "content": "Designing a new harbour game tonight.",
                "metadata": {"ts": 1.0, "topic": "games", "orbit": [core], "orbit_distances": {core: 0.31}}},
               {"id": "s2", "content": "Also a thought about dice.", "metadata": {"ts": 2.0, "topic": "games"}}]
    cands = h._candidates(entries)
    prompt = h._aging_prompt("games", entries, [], cands, evidence=2, allowed_new=False, steer="")
    assert f"[0] (01 Jan 1970" in prompt or "[0] (" in prompt
    assert f"ORBITS core {core}." in prompt, prompt
    assert "[1] (" in prompt and "ORBITS" not in prompt.split("[1] (")[1].split("\n")[0]
    assert "(memory [0] ORBITS this core: written close to it)" in prompt
    assert "A memory that ORBITS a core was written close to it" in prompt
    assert PROMPT_VERSION == "2026-10-30"


def test_no_orbit_no_orbit_talk(memory, make_hippo):
    h = make_hippo()
    _core(memory, "The user makes push-your-luck board games about flooding harbours.")
    h._mem.search_long = lambda q, n=5: []
    entries = [{"id": "s1", "content": "Designing a new harbour game tonight.", "metadata": {"ts": 1.0, "topic": "games"}}]
    prompt = h._aging_prompt("games", entries, [], h._candidates(entries), evidence=1, allowed_new=False, steer="")
    assert "ORBIT" not in prompt


def test_a_replay_keeps_the_orbit(memory, make_hippo):
    seen = {}

    def model(prompt):
        return '{"operations": [{"kind": "age_out", "aging": [0], "rationale": "one off"}]}'
    h = make_hippo(model=model)
    core = _core(memory, "The user makes push-your-luck board games about flooding harbours.")
    h._mem.search_long = lambda q, n=5: []
    h._capture = []
    entries = [{"id": "s1", "content": "Designing a new harbour game tonight.",
                "metadata": {"ts": 1.0, "topic": "games", "orbit": [core], "orbit_distances": {core: 0.31}}}]
    h._decide_pile("games", entries, [], h._candidates(entries), evidence=1, allowed_new=False)
    saved = h._capture[0]["entries"][0]["metadata"]
    assert saved["orbit"] == [core] and saved["orbit_distances"] == {core: 0.31}


def test_a_memory_that_writes_orbits_hands_them_to_the_sleep(memory, make_hippo):
    feats = memory.get("/health").json().get("features") or []
    if "orbit" not in feats:
        pytest.skip("the installed Memory predates orbits")
    h = make_hippo()
    core = _core(memory, "The user makes push-your-luck tile board games about flooding harbours.")
    sid = memory.post("/short", json={"content": "The user is making a push-your-luck tile board game about a flooding harbour tonight.",
                                      "topic": "games"}).json()["id"]
    rows = h._mem.shorts()
    mine = next(r for r in rows if r["id"] == sid)
    assert mine["metadata"]["orbit"] == [core]
    got = h._candidates([mine])
    assert got[0]["id"] == core and got[0]["orbit_of"] == [0]
