"""
A redraft may withdraw an operation, or change its kind and target.

Seen live 27 Sept 2026: two operations aimed at the wrong cores came back
reworded against the SAME cores - one read 'this does not supersede 5504ed41'
while being a supersession of 5504ed41 - and the reviewer's only way out was
to deny until the chain was spent. Pinned here:

- the redraft prompt shows the nearest real cores (the old target always
  among them) and offers withdraw
- withdraw drops the op; a chain whose every denied op is withdrawn ends and
  closes (chain_ended why=withdrawn)
- the kind may change among new_core / attach / supersede; new_core has no
  target; verbatim is the reviewer's mark, never the worker's choice
- a target the worker was not shown is a model failure: the chain waits for
  the next tend rather than landing a guess
- without being told otherwise, kind and target hold (the old answer shape)
"""
from __future__ import annotations

from conftest import as_json, review, short


def _core(memory, content: str, topic: str = "t") -> str:
    did = memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": content, "topic": topic}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _denied_chain(memory, h, op: dict, critique: str = "DENIED: wrong core") -> str:
    """A draft with one operation, denied - as a sleep would leave it."""
    a = short(memory, "the model runs on 7200", "t")
    did = memory.post("/drafts", json={"operations": [{**op, "source_short_ids": [a], "topic": "t"}],
                                       "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": critique}])
    return did


def _pin_candidates(h, cands):
    h._candidates = lambda entries: [dict(c) for c in cands]   # type: ignore[method-assign]


def test_the_prompt_shows_real_cores_and_offers_withdraw(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"content": "reworded", "rationale": "x"})
    h = make_hippo(model=model)
    wrong = _core(memory, "A sleep fires when a brief is open.")
    _denied_chain(memory, h, {"kind": "supersede", "content": "the model runs on 7200", "target_core_id": wrong})
    h.tend()
    p = prompts[-1]
    assert f"({wrong})" in p, "the old target is always among the cores shown"
    assert '"withdraw": true' in p and "Existing cores:" in p


def test_a_withdrawn_op_is_dropped_and_an_all_withdrawn_chain_ends(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json({"withdraw": True, "rationale": "already covered"}))
    wrong = _core(memory, "A sleep fires when a brief is open.")
    did = _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"] == [did] and did in t["closed"]
    assert t["withdrawn"] == {did: 1} and t["model_failures"] == []
    ended = [e for e in h.state["events"] if e["event"] == "chain_ended"]
    assert ended[-1]["why"] == "withdrawn"
    assert not h._open_chain()


def test_a_redraft_can_move_an_op_to_the_right_core_and_kind(memory, make_hippo):
    wrong = _core(memory, "A sleep fires when a brief is open.")
    right = _core(memory, "The hippocampus drafts on a small local model.")
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": right,
                                             "content": "now on the 4B at 7200", "rationale": "right core"}))
    _pin_candidates(h, [{"id": wrong, "content": "A sleep fires...", "topic": "t"},
                        {"id": right, "content": "The hippocampus drafts...", "topic": "t"}])
    _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    t = h.tend()
    op = memory.get(f"/drafts/{t['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert (op["kind"], op["target_core_id"]) == ("attach", right)


def test_a_redraft_can_become_a_new_core(memory, make_hippo):
    wrong = _core(memory, "A sleep fires when a brief is open.")
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "target_core_id": wrong,
                                             "content": "The model runs on 7200.", "rationale": "its own fact"}))
    _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["kind"] == "new_core" and op["target_core_id"] is None, "a new core has no target"


def test_a_target_it_was_not_shown_is_a_failure_and_the_chain_waits(memory, make_hippo):
    wrong = _core(memory, "A sleep fires when a brief is open.")
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": "0" * 32,
                                             "content": "x", "rationale": "made up"}))
    _pin_candidates(h, [{"id": wrong, "content": "A sleep fires...", "topic": "t"}])
    did = _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"] == [] and did not in t["closed"]
    assert "not shown" in t["model_failures"][0]["why"]
    assert h._open_chain(), "it waits for the next tend"


def test_the_old_answer_shape_keeps_kind_and_target(memory, make_hippo):
    target = _core(memory, "The hippocampus drafts on a small local model.")
    h = make_hippo(model=lambda p: as_json({"content": "The hippocampus drafts on the 4B.", "rationale": "x"}))
    _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": target},
                  critique="DENIED: say which model")
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert (op["kind"], op["target_core_id"]) == ("supersede", target)


def test_the_prompt_says_a_wrong_target_moves_not_withdraws(memory, make_hippo):
    """Seen live 28 Sept 2026: every critique said 'wrong target, move it',
    and a prompt that listed 'the wrong core' as a reason to withdraw made a
    4B withdraw all six."""
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"content": "reworded", "rationale": "x"})
    h = make_hippo(model=model)
    wrong = _core(memory, "A sleep fires when a brief is open.")
    _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    h.tend()
    p = prompts[-1]
    assert "A wrong target is not a reason to withdraw" in p
    assert "the wrong core, or already covered - withdraw" not in p


def test_a_new_core_written_in_restated_content_is_kept(memory, make_hippo):
    """Seen live 28 Sept 2026: the corrected text came back in
    restated_content with no content, and the op vanished."""
    wrong = _core(memory, "A sleep fires when a brief is open.")
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "restated_content": "The model runs on 7200.",
                                             "rationale": "its own fact"}))
    _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert (op["kind"], op["content"]) == ("new_core", "The model runs on 7200.")


def test_an_answer_with_no_usable_op_is_said_not_swallowed(memory, make_hippo):
    """JSON with nothing to land used to vanish without a word. It still ends
    the chain (test_bedtime pins why), but the log says what was dropped."""
    wrong = _core(memory, "A sleep fires when a brief is open.")
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "rationale": "forgot the text"}))
    said: list[str] = []
    h._log = said.append
    did = _denied_chain(memory, h, {"kind": "supersede", "content": "x", "target_core_id": wrong})
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"] == [did]
    assert any("no usable operation" in m and "forgot the text" in m for m in said)


def test_a_verbatim_the_reviewer_marked_is_not_the_workers_to_reword(memory, make_hippo):
    """Word for word is the reviewer's call (preserve_memory_verbatim): 'I
    want this word for word, like a promise' (Chad, 2 Oct 2026). Denied, it
    is not rewritten by the worker; it leaves the chain and stays marked."""
    calls: list[str] = []
    said: list[str] = []
    h = make_hippo(model=lambda p: calls.append(p) or as_json({"kind": "new_core", "content": "reworded"}))
    h._log = said.append
    a = short(memory, "never piss on a electric fence", "t")
    memory.post(f"/short/{a}/preserve")
    did = memory.post("/drafts", json={"operations": [
        {"kind": "verbatim", "content": "never piss on a electric fence", "topic": "t", "source_short_ids": [a]}],
        "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "not now"}])
    t = h.tend()
    assert calls == [], "the model is never asked to reword it"
    assert t["resubmitted"] == [] and t["ended"] == [did] and t["withdrawn"] == {did: 1}
    assert any("a verbatim the reviewer marked" in m for m in said)
    kept = [e for e in memory.get("/short", params={"limit": 50}).json()["entries"] if e["id"] == a]
    assert kept and kept[0]["metadata"].get("verbatim"), "the memory stays, still marked"


def test_a_verbatim_the_worker_chose_is_an_ordinary_operation(memory, make_hippo):
    """Nobody marked it: an earlier worker picked the kind. It redrafts like
    anything else, and does not come back claiming to be word for word."""
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "content": "Never piss on an electric fence.",
                                             "rationale": "x"}))
    _denied_chain(memory, h, {"kind": "verbatim", "content": "never piss on a electric fence"},
                  critique="DENIED: not verbatim, nobody said exactly this; state the lesson")
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["kind"] == "new_core" and op["content"] == "Never piss on an electric fence."


def test_the_drafting_worker_cannot_choose_verbatim(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"operations": [{"kind": "verbatim", "content": "the model runs on 7200", "rationale": "exact",
                                        "source_indexes": [0]}]})
    h = make_hippo(model=model, promote_min_evidence=1)
    short(memory, "the model runs on 7200", "t")
    got = memory.get(f"/drafts/{h.sleep()['draft_id']}").json()["operations"]
    assert [o["kind"] for o in got] == ["new_core"], "what it wrote is kept, as an ordinary core"
    assert "new_core|attach|supersede\"" in prompts[-1] and "verbatim" not in prompts[-1].split("Fragments:")[0]


def test_a_marked_memory_still_lands_word_for_word(memory, make_hippo):
    """The mark is untouched by all of this: no model, the exact text."""
    h = make_hippo(model=lambda p: as_json({"operations": []}), promote_min_evidence=1)
    a = short(memory, "ill always be here with a lantern", "t")
    memory.post(f"/short/{a}/preserve")
    op = memory.get(f"/drafts/{h.sleep()['draft_id']}").json()["operations"][0]
    assert (op["kind"], op["content"], op["source_short_ids"]) == ("verbatim", "ill always be here with a lantern", [a])
