"""
The brief is the gate.

- check: an open brief and no open chain -> sleep on it, now, any hour
- no brief before bedtime -> nothing; past bedtime the misses are counted and
  every brief_check_misses of them the hippocampus asks (a near-term intent in
  Memory, a brief_requested event); asked and waiting, it nudges, no second note
- one chain at a time: a brief that arrives during a review waits
- the cull: when the chain lands, tend closes the drafts, consumes the brief,
  completes the review note, emits chain_closed; the next check finds no brief
- a brief with nothing to draft from is consumed at once
- brief_pull off: no brief, no draft, ever; on: the small model pulls after
  the misses add up
- purge runs on the tick even with no brief
"""
from __future__ import annotations

import time

from conftest import as_json, review, short


def _slept(h, ago_seconds: float) -> None:
    h.state["last_sleep"] = {"finished_at": time.time() - ago_seconds, "error": None}


def _notes(memory, word: str) -> list[dict]:
    return [e for e in memory.get("/near").json()["entries"] if word in (e.get("content") or "")]


def _brief(memory, summary="what mattered", promote=None):
    return memory.post("/brief", json={"summary": summary, "promote_hints": promote or [], "noise_hints": []}).json()["id"]


def test_a_brief_opens_a_sleep_at_any_hour(memory, make_hippo):
    h = make_hippo(interval_seconds=72000)
    _slept(h, 60)                                          # nowhere near bedtime
    short(memory, "a", "t"); short(memory, "b", "t")
    assert h.check()["status"] == "not_bedtime"
    bid = _brief(memory, "I'm off to the forest; the t stuff mattered")
    rep = h.check()
    assert rep["status"] == "sleeping" and rep["brief_id"] == bid
    assert rep["sleep"]["draft_id"] and rep["sleep"]["brief_id"] == bid and rep["sleep"]["brief_pulled"] is False
    assert _notes(memory, "waits for your review"), "the model is poked to review"


def test_no_brief_no_draft_and_the_call_comes_after_the_misses(memory, make_hippo):
    h = make_hippo(interval_seconds=3600, brief_check_misses=3)
    short(memory, "a", "t"); short(memory, "b", "t")
    _slept(h, 60)
    assert h.check()["status"] == "not_bedtime" and h.state["brief_misses"] == 0
    _slept(h, 4000)                                        # past bedtime
    assert [h.check()["status"] for _ in range(2)] == ["waiting_for_brief"] * 2
    assert _notes(memory, "wants a brief") == [], "two misses: not yet"
    rep = h.check()
    assert rep["misses"] == 3 and len(_notes(memory, "wants a brief")) == 1
    assert h.brief_wanted() and h.state["events"][-1]["event"] == "brief_requested"
    for _ in range(3):
        h.check()
    assert len(_notes(memory, "wants a brief")) == 1, "asked again: a nudge, not a second note"
    assert h.state["events"][-1]["event"] == "brief_requested" and h.state["events"][-1]["nudge"] == 1
    assert memory.get("/drafts").json()["count"] == 0, "no brief, no draft"


def test_the_cull_closes_drafts_consumes_the_brief_and_retires_the_note(memory, make_hippo):
    h = make_hippo()
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory)
    rep = h.check()
    did = rep["sleep"]["draft_id"]
    assert h.check()["status"] == "chain_open", "the brief is still open; the chain is under review"
    assert memory.get("/brief").json()["count"] == 1
    review(memory, did, [{"op": 0, "verdict": "approve"}])
    t = h.tend()
    assert t["closed"] == [did]
    assert memory.get(f"/drafts/{did}").json()["status"] == "closed"
    assert memory.get("/brief").json()["count"] == 0, "consumed: the check will not see it again"
    hist = memory.get("/brief", params={"include_consumed": "true"}).json()["entries"]
    assert hist[0]["id"] == bid and hist[0]["metadata"]["consumed_by_draft"] == did
    assert _notes(memory, "waits for your review") == [], "the review note is retired"
    assert "waits for your review" not in memory.get("/long").text, (
        "the poke is not filed in long-term as a completed intent (the tidy ran at the close)")
    kinds = [e["event"] for e in h.state["events"]]
    assert "chain_closed" in kinds and "brief_consumed" in kinds
    assert memory.get("/drafts", params={"status": "reviewed"}).json()["count"] == 0
    assert h.check()["status"] in ("not_bedtime", "waiting_for_brief"), "back to checking"


def test_a_denied_chain_is_redrafted_then_culled_when_it_lands(memory, make_hippo):
    def model(prompt: str) -> str:
        return as_json({"content": "again", "rationale": "x"}) if "DENIED" in prompt else \
            as_json({"operations": [{"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model, max_attempts=2)
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory)
    first = h.check()["sleep"]["draft_id"]
    review(memory, first, [{"op": 0, "verdict": "deny", "critique": "no"}])
    t = h.tend()
    second = t["resubmitted"][0]["draft_id"]
    assert t["closed"] == [], "still open"
    assert h.check()["status"] == "chain_open"
    review(memory, second, [{"op": 0, "verdict": "approve"}])
    t = h.tend()
    assert t["closed"] == [first]
    assert memory.get(f"/drafts/{first}").json()["status"] == "closed"
    assert memory.get(f"/drafts/{second}").json()["status"] == "closed"
    assert memory.get("/brief").json()["count"] == 0
    assert memory.get("/brief", params={"include_consumed": "true"}).json()["entries"][0]["id"] == bid


def test_a_brief_with_nothing_to_draft_from_is_consumed_at_once(memory, make_hippo):
    h = make_hippo()
    _brief(memory, "quiet day")
    rep = h.check()
    assert rep["status"] == "sleeping" and rep["sleep"]["quiet"] is True
    assert memory.get("/brief").json()["count"] == 0
    assert "brief_consumed" in [e["event"] for e in h.state["events"]]


def test_brief_pull_is_off_by_default_and_on_it_sleeps_after_the_misses(memory, make_hippo):
    pulls: list[str] = []

    def model(prompt: str) -> str:
        if "JSON brief" in prompt:
            pulls.append(prompt)
            return as_json({"summary": "guessed", "promote_hints": [], "noise_hints": [], "completed_intents": []})
        return as_json({"operations": []})
    h = make_hippo(model=model, interval_seconds=3600, brief_check_misses=2)
    short(memory, "a", "t"); short(memory, "b", "t")
    _slept(h, 4000)
    for _ in range(4):
        h.check()
    assert pulls == [] and memory.get("/drafts").json()["count"] == 0
    h2 = make_hippo(model=model, interval_seconds=3600, brief_check_misses=2, brief_pull=True)
    _slept(h2, 4000)
    h2.state["brief_misses"] = 0                           # h2 shares h's state file; start its count fresh
    assert h2.check()["status"] == "waiting_for_brief"
    rep = h2.check()
    assert rep["status"] == "sleeping_on_pulled_brief" and rep["sleep"]["brief_pulled"] is True and pulls


def test_flagged_memories_are_purged_on_the_tick_without_a_brief(memory, make_hippo):
    h = make_hippo()
    core = review(memory, memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "the key is ssh-rsa AAAA", "topic": "oops"}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    h.tend()                                               # culls that hand-made chain
    memory.post(f"/long/{core}/forget", json={"reason": "leaked key"})
    rep = h.check()
    assert rep["sleep"] is None
    assert h.state["events"][-1]["event"] == "purged" and h.state["events"][-1]["ids"] == [core]


def test_the_app_ticks_and_reports_the_gate(memory, bridge, hcfg):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    hcfg.sleep.interval_seconds = 3600
    hcfg.sleep.brief_check_misses = 1
    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        h = app.state.hippocampus
        _slept(h, 4000)
        assert tc.post("/check").json()["status"] == "waiting_for_brief"
        st = tc.get("/status").json()
        assert st["brief_wanted"] is True and st["brief_misses"] == 1 and st["bedtime_at"] < time.time()
        assert st["last_check"]["status"] == "waiting_for_brief" and st["next_sleep_at"] is None
        short(memory, "a", "t"); short(memory, "b", "t")
        _brief(memory)
        assert tc.post("/check").json()["status"] == "sleeping"
        assert tc.get("/queue").json()["count"] == 1
    client.close()


def test_an_older_open_brief_retires_with_the_one_the_sleep_used(memory, make_hippo):
    """Seen live 25 Sept 2026: two briefs written the same night, the sleep
    used the newer, the cull consumed only that, and the older would have
    opened a second sleep under an outdated steer. A brief written after the
    sleep started is the next sleep's and stays open."""
    import time as _t
    h = make_hippo()
    short(memory, "a", "t"); short(memory, "b", "t")
    older = _brief(memory, "early in the night")
    _t.sleep(0.05)
    newer = _brief(memory, "end of the night")
    rep = h.check()
    assert rep["sleep"]["brief_id"] == newer
    _t.sleep(0.05)
    later = _brief(memory, "tomorrow's")
    review(memory, rep["sleep"]["draft_id"], [{"op": 0, "verdict": "approve"}])
    h.tend()
    open_ids = [b["id"] for b in memory.get("/brief").json()["entries"]]
    assert older not in open_ids and newer not in open_ids, "the used brief and the one it superseded are retired"
    assert open_ids == [later], "a brief written after the sleep started stays for the next one"
    hist = {b["id"]: b["metadata"] for b in memory.get("/brief", params={"include_consumed": "true"}).json()["entries"]}
    assert hist[older]["consumed_by_draft"] == f"superseded-by-{newer}"


def test_the_draft_cap_is_held_and_one_attempt_means_terminal(memory, make_hippo):
    """The draft cap - max rounds of draft and critique, so no
    endless loop - lives here. 0 would be no draft at all and 999 a chain that
    runs until the reviewer gives up, so it is held to 1-10; bedtime is held
    to at least ten minutes. max_attempts 1 means the first draft is the last:
    submitted terminal, so a denial ends the chain with no redraft."""
    from seren_hippocampus.config import SleepConfig
    assert SleepConfig(max_attempts=0).max_attempts == 1
    assert SleepConfig(max_attempts=999).max_attempts == 10
    assert SleepConfig(interval_seconds=5).interval_seconds == 600

    h = make_hippo(model=lambda p: as_json({"operations": [
        {"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]}))
    h._cfg.sleep.max_attempts = 1
    short(memory, "a", "t"); short(memory, "b", "t")
    memory.post("/brief", json={"summary": "t", "promote_hints": ["t"], "noise_hints": []})
    did = h.check()["sleep"]["draft_id"]
    assert memory.get(f"/drafts/{did}").json()["terminal"] is True
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "no"}])
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"], "no redraft past the cap: the chain ended"


def test_a_sleep_by_hand_waits_for_the_open_chain(memory, make_hippo):
    """One sleep cycle at a time - no second draft, no second
    brief consumed, while one is under review. The tick always waited; a sleep
    by hand (the button, POST /sleep, the MCP sleep_now) did not. It refuses
    now, records no sleep (bedtime does not move), and works again once the
    chain has landed."""
    h = make_hippo(model=lambda p: as_json({"operations": [
        {"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]}))
    short(memory, "a", "t"); short(memory, "b", "t")
    memory.post("/brief", json={"summary": "t", "promote_hints": ["t"], "noise_hints": []})
    did = h.check()["sleep"]["draft_id"]
    last = h.state.get("last_sleep")

    short(memory, "c", "u"); short(memory, "d", "u")
    r = h.sleep()
    assert r["refused"] and r["status"] == "chain_open" and r["draft_id"] is None
    assert len(memory.get("/drafts", params={"status": "pending"}).json()["entries"]) == 1, "no second draft"
    assert h.state.get("last_sleep") == last, "a refusal is not a sleep: bedtime does not move"

    review(memory, did, [{"op": 0, "verdict": "approve"}])
    h.tend()                                           # the chain lands and closes
    assert not h.sleep().get("refused"), "once it has landed, a sleep by hand runs"


def test_a_denied_op_whose_shorts_a_sibling_promoted_is_still_redrafted(memory, make_hippo):
    """Seen live 27 Sept 2026: ops 3 and 4 shared a short-term; approving op 4
    promoted it, and the redraft silently dropped the denied op 3 - the
    correction the critique asked for was lost. It is redrafted from the
    previous content and the critique, and keeps its source ids."""
    prompts: list[str] = []

    def model(p: str) -> str:
        prompts.append(p)
        if "DENIED" in p:
            return as_json({"content": "the hippocampus runs on 7269", "rationale": "from the critique"})
        return as_json({"operations": [
            {"kind": "new_core", "content": "the hippocampus runs on 7200", "rationale": "x", "source_indexes": [0, 1]},
            {"kind": "new_core", "content": "reinstall keeps the token", "rationale": "x", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model)
    a = short(memory, "hippo port and the token", "t"); b = short(memory, "hippo port and the token again", "t")
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: 7200 is the model; the hippocampus is 7269"},
                         {"op": 1, "verdict": "approve"}])     # promotes a and b
    t = h.tend()
    assert len(t["resubmitted"]) == 1 and t["resubmitted"][0]["operations"] == 1, t
    op = memory.get(f"/drafts/{t['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["content"] == "the hippocampus runs on 7269"
    assert set(op["source_short_ids"]) == {a, b}, "provenance kept"
    assert "already promoted them" in prompts[-1]


def test_a_chain_that_can_redraft_nothing_ends_instead_of_holding_every_sleep(memory, make_hippo):
    """A tend that produces no redraft while the model is fine can never move
    the chain again; left open, one-cycle-at-a-time would refuse every sleep
    and the age-out would wait forever. It ends and closes."""
    h = make_hippo(model=lambda p: as_json({"content": "", "rationale": "nothing to say"}) if "DENIED" in p
                   else as_json({"operations": [
                       {"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]}))
    short(memory, "a", "t"); short(memory, "b", "t")
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: no"}])
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"] == [did] and t["closed"] == [did]
    assert "near" in t["tidy"][did], "the cycle ended, so its tidy ran"
    assert not h._open_chain()
    assert not h.sleep().get("refused")


def test_a_model_failure_in_a_redraft_leaves_the_chain_waiting(memory, make_hippo):
    """The other side: the model answering garbage is a failure, and the chain
    waits for the next tend rather than ending on a bad minute."""
    h = make_hippo(model=lambda p: "not json at all" if "DENIED" in p else as_json({"operations": [
        {"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]}))
    short(memory, "a", "t"); short(memory, "b", "t")
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: no"}])
    t = h.tend()
    assert t["ended"] == [] and t["closed"] == [] and t["model_failures"]
    assert h._open_chain()


def test_aging_out_is_at_the_end_of_the_cycle_not_under_a_draft(memory, make_hippo):
    """The hippocampus ages out short-terms, always at the
    end of a sleep - and the sleep is the whole cycle. A short-term past its
    lifetime survives while a draft is out (even one whose operation was
    denied and waits for a redraft), and ages out when the chain lands."""
    h = make_hippo(model=lambda p: as_json({"content": "x, reworded", "rationale": "the critique"})
                   if "DENIED" in p else as_json({"operations": [
                       {"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]}))
    h.state["last_sleep"] = {"finished_at": time.time() - 60, "error": None}   # not a catch-up
    a = short(memory, "a", "t"); b = short(memory, "b", "t")
    rep = h.sleep()
    assert rep["draft_id"] and rep["tidy"] == {"deferred": "until the chain lands"}

    store = memory.app.state.store
    review(memory, rep["draft_id"], [{"op": 0, "verdict": "deny", "critique": "DENIED: say more"}])
    got = store.short.get(ids=[a, b], include=["metadatas"])
    store.short.update(ids=got["ids"], metadatas=[{**m, "ts": time.time() - 90 * 86400}   # past any lifetime
                                                  for m in got["metadatas"]])
    t1 = h.tend()                                      # the redraft goes out; nothing ages out
    assert t1["resubmitted"] and not t1["tidy"]
    ids = {r["id"] for r in store.get_short_all(limit=None)}
    assert {a, b} <= ids, "denied and awaiting its redraft: not aged out"

    redraft = t1["resubmitted"][0]["draft_id"]
    review(memory, redraft, [{"op": 0, "verdict": "deny", "critique": "DENIED: still no"}])
    t2 = h.tend()
    while not t2["closed"]:                            # spend the chain
        nxt = t2["resubmitted"][0]["draft_id"]
        review(memory, nxt, [{"op": 0, "verdict": "deny", "critique": "DENIED: no"}])
        t2 = h.tend()
    cid = t2["closed"][0]
    assert t2["tidy"][cid]["aged_out"] == 2, "the chain landed: the cycle ends with its age-out"
    ids = {r["id"] for r in store.get_short_all(limit=None)}
    assert not ({a, b} & ids)
