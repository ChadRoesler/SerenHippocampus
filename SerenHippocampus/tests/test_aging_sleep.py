"""
The sleep that touches only what is aging (design note, 22 Oct 2026).

Short-term is the quick access - this week's things - and it ranks above
long-term in recall, so it stays whole for its week. A sleep decides about the
memories that reach the end of their week before the next sleep, and nothing
else; the evidence for keeping one is weighed against the rest of short-term
and the cores that exist. Each aging memory gets a verdict: support a core (it
becomes a satellite), refine one (a small change), replace one (the core was
wrong, and the replace quotes where), a new core (past the evidence bar), or
age out.

Pinned here:
- only aging memories are decided; the younger ones on the topic are shown as
  context and stay in short-term after the chain lands
- the aging memories are shown whole and dated; context is cut visibly
- the evidence bar counts all of short-term: seven memories on a topic, two of
  them aging, clear it; a lone memory with nothing to attach to does not, and
  a new core proposed for it is dropped and it ages out as a verdict
- a memory the model leaves out gets an explicit age_out (the j in the
  drawing); a pin never ages out; a pile whose model call failed gets no
  verdicts made up for it, and nothing leaves short-term silently
- support keeps the memory itself under the core; refine rewords the core; a
  replace must quote the sentence it proves wrong or it is support
- a brief's hint is context in this flow: it does not lower the bar
- a redraft of an aging memory: a withdrawal is an age_out, an attach needs no
  text, a supersede with no quote is an attach unless the critique asked
- with no model: a topic over the bar becomes a core, one under it ages out
- a replay of an aging draft is judged the way the sleep judged it
"""
from __future__ import annotations

import re
import time

from conftest import as_json, cores, review, short

DAY = 24 * 3600


def _aged(memory, content, topic, days=8.0, **extra) -> str:
    return short(memory, content, topic, ts=time.time() - days * DAY, **extra)


def _core(memory, content, topic="t") -> str:
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": content, "topic": topic}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _core_ids(prompt: str) -> list[str]:
    return re.findall(r"\(([0-9a-f]{32})\)", prompt.split("Existing cores:")[1]) if "Existing cores:" in prompt else []


def _aging(make_hippo, model=None, **kw):
    kw.setdefault("promote_min_evidence", 3)
    return make_hippo(model=model, flow="aging", **kw)


def _ops(memory, draft_id):
    return memory.get(f"/drafts/{draft_id}").json()["operations"]


def _in_short(memory) -> set[str]:
    return {e["id"] for e in memory.get("/short", params={"limit": 500}).json()["entries"]}


# ── only what is aging ────────────────────────────────────────────────────────
def test_only_the_aging_memories_are_decided_and_the_week_stays(memory, make_hippo):
    seen: list[str] = []

    def model(prompt):
        seen.append(prompt)
        return as_json({"operations": [{"kind": "new_core", "aging": [0, 1],
                                        "content": "Alice lights the lantern on the porch and tells a story by it, "
                                                   "and always carries one.",
                                        "rationale": "a running bit"}]})
    h = _aging(make_hippo, model)
    a1 = _aged(memory, "Alice lit the lantern on the porch and told a story by it.", "lantern, alice", days=8.2)
    a2 = _aged(memory, "The lantern again: Alice always carries one.", "lantern, alice", days=7.6)
    young = [short(memory, f"lantern story number {i}, by the fire", "lantern, alice") for i in range(3)]
    rep = h.sleep()
    assert rep["aging"] == 2 and rep["draft_id"]
    prompt = seen[0]
    head, rest = prompt.split("At the end of their week (decide each):")
    aging_part, context_part = rest.split("Newer memories on this topic (context, they stay):")
    assert "porch" in aging_part and "always carries one" in aging_part
    assert all(f"number {i}" in context_part for i in range(3)) and "number" not in aging_part
    assert "5 memories in short-term are about this topic (2 at the end of their week, 3 newer)" in prompt
    assert "clears the bar" in prompt
    ops = _ops(memory, rep["draft_id"])
    assert len(ops) == 1 and ops[0]["kind"] == "new_core" and sorted(ops[0]["source_short_ids"]) == sorted([a1, a2])
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve"}])
    h.tend()
    assert set(young) <= _in_short(memory), "this week stays in short-term"
    assert not ({a1, a2} & _in_short(memory)), "the aging ones went to long, kept under their core"


def test_the_aging_memories_are_whole_and_dated_and_context_is_cut_visibly(memory, make_hippo):
    seen: list[str] = []
    h = _aging(make_hippo, lambda p: seen.append(p) or as_json({"operations": []}), context_chars=80)
    long_text = "On the UI day " + "we pushed one more pass of styling and it all deployed fine, " * 14 + "the end."
    _aged(memory, long_text, "ui, deploy")
    short(memory, "a younger memory about the ui deploy " + "that goes on and on " * 10, "ui, deploy")
    h.sleep()
    assert long_text in seen[0], "never cut: the model copied the cut faithfully and was denied for it"
    assert re.search(r"\[0\] \(\d\d \w\w\w \d{4} \d\d:\d\d\)", seen[0]), "the memory's date rides with it"
    assert "[...cut]" in seen[0].split("Newer memories on this topic")[1]


# ── the evidence bar counts all of short-term ─────────────────────────────────
def test_a_lone_memory_with_nowhere_to_go_ages_out_as_a_verdict(memory, make_hippo):
    seen: list[str] = []

    def model(prompt):
        seen.append(prompt)
        return as_json({"operations": [{"kind": "new_core", "aging": [0], "content": "A melon was bought.",
                                        "rationale": "keep it"}]})
    h = _aging(make_hippo, model)
    m = _aged(memory, "Bought a melon at the corner shop.", "groceries")
    rep = h.sleep()
    assert "under the bar of 3: no new_core" in seen[0]
    ops = _ops(memory, rep["draft_id"])
    assert [(o["kind"], o["source_short_ids"]) for o in ops] == [("age_out", [m])]
    assert "under the evidence bar" in ops[0]["rationale"], "the reviewer is told why"
    res = review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve"}])["results"][0]
    assert res["kind"] == "age_out" and m not in _in_short(memory)


def test_a_memory_the_model_leaves_out_gets_an_explicit_age_out(memory, make_hippo):
    """The j in the drawing: every aging memory gets a verdict."""
    core = _core(memory, "Alice's board-game nights run late.", "games")

    def model(prompt):
        return as_json({"operations": [{"kind": "support", "aging": [0], "target_core_id": _core_ids(prompt)[0]}]})
    h = _aging(make_hippo, model)
    a = _aged(memory, "Board-game night ran to 3 a.m. again.", "games")
    j = _aged(memory, "The game shop changed its hours.", "games")
    rep = h.sleep()
    by_kind = {o["kind"]: o for o in _ops(memory, rep["draft_id"])}
    assert by_kind["attach"]["source_short_ids"] == [a] and by_kind["attach"]["target_core_id"] == core
    assert by_kind["age_out"]["source_short_ids"] == [j]


# ── support, refine, replace ──────────────────────────────────────────────────
def test_support_keeps_the_memory_itself_under_the_core(memory, make_hippo):
    core = _core(memory, "Alice found red berries with faces in the woods.", "berries")
    h = _aging(make_hippo, lambda p: as_json({"operations": [{"kind": "support", "aging": [0],
                                                              "target_core_id": _core_ids(p)[0],
                                                              "content": "a summary nobody asked for"}]}))
    s = _aged(memory, "Alice boiled some of the face berries; still fine.", "berries")
    rep = h.sleep()
    op = _ops(memory, rep["draft_id"])[0]
    assert op["kind"] == "attach" and op["content"] == "" and op["target_core_id"] == core
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve"}])
    sats = memory.get(f"/long/{core}/satellites").json()["satellites"]
    assert [x["content"] for x in sats] == ["Alice boiled some of the face berries; still fine."]


def test_a_refine_rewords_the_core(memory, make_hippo):
    core = _core(memory, "The workbench is cool grey.", "workbench")
    h = _aging(make_hippo, lambda p: as_json({"operations": [{"kind": "refine", "aging": [0],
                                                              "target_core_id": _core_ids(p)[0],
                                                              "restated_content": "The workbench is light aqua."}]}))
    _aged(memory, "Painted the workbench light aqua today.", "workbench")
    rep = h.sleep()
    op = _ops(memory, rep["draft_id"])[0]
    assert op["kind"] == "attach" and op["restated_content"] == "The workbench is light aqua."
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve", "restate": True}])
    assert cores(memory)[core]["content"] == "The workbench is light aqua."


def _replace_with(quote):
    return lambda p: as_json({"operations": [{
        "kind": "replace", "aging": [0], "target_core_id": _core_ids(p)[0],
        "content": "Red berries with faces turned Alice's skin blue; they are terrible for you.",
        "contradicts": quote}]})


def test_a_replace_that_quotes_the_core_is_a_supersede(memory, make_hippo):
    core = _core(memory, "Red berries with faces are good to eat.", "berries")
    h = _aging(make_hippo, _replace_with("Red berries with faces are good to eat."))
    _aged(memory, "Three weeks on, the face berries turned Alice's skin blue.", "berries")
    op = _ops(memory, h.sleep()["draft_id"])[0]
    assert op["kind"] == "supersede" and op["target_core_id"] == core
    assert op["contradicts"] == "Red berries with faces are good to eat."


def test_a_replace_that_quotes_nothing_the_core_says_is_support(memory, make_hippo):
    core = _core(memory, "Red berries with faces are good to eat.", "berries")
    h = _aging(make_hippo, _replace_with("Berries are a kind of fruit, botanically."))
    _aged(memory, "Three weeks on, the face berries turned Alice's skin blue.", "berries")
    op = _ops(memory, h.sleep()["draft_id"])[0]
    assert op["kind"] == "attach" and op["content"] == "" and op["target_core_id"] == core


# ── pins, failures, and nothing leaving silently ──────────────────────────────
def test_a_pin_never_ages_out(memory, make_hippo):
    h = _aging(make_hippo, lambda p: as_json({"operations": [{"kind": "age_out", "aging": [0]}]}))
    p = _aged(memory, "The lantern promise.", "promise", pinned=True)
    rep = h.sleep()
    assert rep["draft_id"] is None and rep["operations"] == 0
    assert p in _in_short(memory)


def test_a_pile_whose_model_call_failed_gets_no_verdicts_and_nothing_leaves(memory, make_hippo):
    def model(prompt):
        raise RuntimeError("the model fell over")
    h = _aging(make_hippo, model)
    m = _aged(memory, "Bought a melon at the corner shop.", "groceries")
    rep = h.sleep()
    assert rep["draft_id"] is None and rep["model_failures"]
    assert m in _in_short(memory), "nothing aged out silently"


def test_the_tidy_at_the_end_of_a_chain_never_ages_out_on_its_own(memory, make_hippo):
    core = _core(memory, "Board-game nights.", "games")
    h = _aging(make_hippo, lambda p: as_json({"operations": [{"kind": "support", "aging": [0],
                                                              "target_core_id": _core_ids(p)[0]}]}))
    a = _aged(memory, "Board-game night ran late again.", "games")
    rep = h.sleep()
    late = _aged(memory, "An old note written after the draft went out.", "other", days=9)
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve"}])
    t = h.tend()
    assert t["closed"] and not t["tidy"][rep["draft_id"]].get("aged_out")
    assert late in _in_short(memory), "it waits for the next sleep's verdict"
    assert a not in _in_short(memory)


# ── the brief is context ──────────────────────────────────────────────────────
def test_a_brief_hint_does_not_lower_the_bar(memory, make_hippo):
    seen: list[str] = []
    h = _aging(make_hippo, lambda p: seen.append(p) or as_json({"operations": []}))
    _aged(memory, "Bought a melon at the corner shop.", "groceries")
    memory.post("/brief", json={"summary": "the melon matters", "promote_hints": ["groceries"]})
    h.sleep()
    assert "under the bar of 3: no new_core" in seen[0]
    assert "a new core still needs the evidence" in seen[0]


# ── the redraft of an aging memory ────────────────────────────────────────────
def _denied_support(memory, make_hippo, redraft_answer):
    core = _core(memory, "Board-game nights.", "games")
    calls = {"n": 0}

    def model(prompt):
        calls["n"] += 1
        if "THE CRITIQUE" in prompt:
            assert "These memories are at the end of their week" in prompt
            return as_json(redraft_answer(prompt))
        return as_json({"operations": [{"kind": "support", "aging": [0], "target_core_id": _core_ids(prompt)[0]}]})
    h = _aging(make_hippo, model)
    a = _aged(memory, "Board-game night ran late again.", "games")
    rep = h.sleep()
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "deny", "critique": "not this core"}])
    t = h.tend()
    return core, a, t["resubmitted"][0]["draft_id"]


def test_a_withdrawn_redraft_of_an_aging_memory_is_an_age_out(memory, make_hippo):
    _, a, again = _denied_support(memory, make_hippo, lambda p: {"withdraw": True, "rationale": "a one-off"})
    ops = _ops(memory, again)
    assert [(o["kind"], o["source_short_ids"], o["redraft_of"]) for o in ops] == [("age_out", [a], 0)]


def test_a_redrafted_attach_needs_no_text(memory, make_hippo):
    core, a, again = _denied_support(memory, make_hippo,
                                     lambda p: {"kind": "attach", "target_core_id": _core_ids(p)[0]})
    op = _ops(memory, again)[0]
    assert op["kind"] == "attach" and op["content"] == "" and op["source_short_ids"] == [a]


def test_a_redrafted_supersede_with_no_quote_is_an_attach(memory, make_hippo):
    core, a, again = _denied_support(memory, make_hippo, lambda p: {
        "kind": "supersede", "target_core_id": _core_ids(p)[0], "content": "Board-game nights ended."})
    op = _ops(memory, again)[0]
    assert op["kind"] == "attach" and op["content"] == ""


# ── no model ──────────────────────────────────────────────────────────────────
def test_with_no_model_over_the_bar_is_a_core_and_under_it_ages_out(memory, make_hippo):
    h = _aging(make_hippo)
    a = [_aged(memory, f"lantern story {i}", "lantern") for i in range(2)]
    short(memory, "a younger lantern story", "lantern")
    m = _aged(memory, "Bought a melon.", "groceries")
    rep = h.sleep()
    kinds = {o["kind"]: o for o in _ops(memory, rep["draft_id"])}
    assert sorted(kinds["new_core"]["source_short_ids"]) == sorted(a)
    assert kinds["age_out"]["source_short_ids"] == [m]


# ── replay ────────────────────────────────────────────────────────────────────
def test_a_replay_of_an_aging_draft_is_judged_the_same_way(memory, make_hippo):
    h = _aging(make_hippo, lambda p: as_json({"operations": [{"kind": "new_core", "aging": [0],
                                                              "content": "A melon was bought."}]}))
    _aged(memory, "Bought a melon at the corner shop.", "groceries")
    rep = h.sleep()
    out = h.replay(rep["draft_id"])
    call = out["candidate"]["calls"][0]
    assert call["stage"] == "decide" and call["ops"] == [], "under the bar on replay too"


# ── the brief is optional (design note, 22 Oct 2026) ──────────────────────────
def _wants_a_brief(memory) -> list[dict]:
    return [e for e in memory.get("/near").json()["entries"] if "wants a brief" in (e.get("content") or "")]


def test_bedtime_with_no_brief_sleeps_without_one_and_asks_nobody(memory, make_hippo):
    h = _aging(make_hippo, lambda p: as_json({"operations": []}), interval_seconds=3600, brief_check_misses=1)
    _aged(memory, "Bought a melon at the corner shop.", "groceries")
    h.state["last_sleep"] = {"finished_at": time.time() - 60, "error": None}
    assert h.check()["status"] == "not_bedtime"
    h.state["last_sleep"] = {"finished_at": time.time() - 4000, "error": None}
    rep = h.check()
    assert rep["status"] == "sleeping_without_brief" and rep["misses"] == 0
    assert rep["sleep"]["draft_id"] and rep["sleep"]["brief_id"] is None
    assert [o["kind"] for o in _ops(memory, rep["sleep"]["draft_id"])] == ["age_out"]
    assert _wants_a_brief(memory) == [] and not h.brief_wanted(), "nobody is woken to write one"
    h.state["last_sleep"] = {"finished_at": time.time() - 4000, "error": None}
    assert h.check()["status"] == "chain_open", "past bedtime again: one chain at a time, brief or not"


def test_a_brief_still_starts_the_sleep_before_bedtime(memory, make_hippo):
    h = _aging(make_hippo, lambda p: as_json({"operations": []}), interval_seconds=72000)
    _aged(memory, "Bought a melon at the corner shop.", "groceries")
    h.state["last_sleep"] = {"finished_at": time.time() - 60, "error": None}
    bid = memory.post("/brief", json={"summary": "the melon was a joke", "promote_hints": [],
                                      "noise_hints": []}).json()["id"]
    rep = h.check()
    assert rep["status"] == "sleeping" and rep["sleep"]["brief_id"] == bid


def test_a_sleep_by_hand_with_no_brief_sleeps_instead_of_asking(memory, make_hippo):
    h = _aging(make_hippo, lambda p: as_json({"operations": []}))
    _aged(memory, "Bought a melon at the corner shop.", "groceries")
    rep = h.sleep(ask_for_brief=True)
    assert rep.get("asked_for_brief") is None and rep["draft_id"]
    assert _wants_a_brief(memory) == []
