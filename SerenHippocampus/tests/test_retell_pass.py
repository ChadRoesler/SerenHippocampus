"""
The retell pass (21 Oct 2026): the fattest cores that still are their episode
get a retelling proposed, as one terminal draft the reviewer lands. Pinned:

- fat cores without an episode under them are proposed, fattest first, up to
  the limit; thin ones and ones already carrying their episode are not
- the draft is terminal, and approving it replaces each core's wording and
  hangs the old wording under it (Memory's retell operation)
- an answer that is unchanged, longer, or ungrounded in the original is
  skipped and counted, not proposed
- no model: refused; a chain open: refused
- the route and the tool answer the same way
"""
from __future__ import annotations

import json

import pytest

from conftest import cores, review, short


def _takes(memory, feature: str) -> bool:
    return feature in (memory.get("/health").json().get("features") or [])


def _needs_retell(memory):
    if not _takes(memory, "retell"):
        pytest.skip("the installed seren-memory predates the retell operation")

FAT = ("12 Oct 2026, late: the garden plan, years in the thinking. The raised beds go along the south fence, three of "
       "them, cedar, with the compost bay at the far end; the neighbour offered the old water butt, and the first plan"
       "ting will be garlic in November, because 'everything else can wait for spring'; the apple tree stays where it "
       "is, and the path gets gravel, not slabs.")
FAT2 = ("20 Oct 2026, the knob sweep: five corpora, the shipped defaults were best or within one question on every one; "
        "weight turned out to be a rank offset k(1/w-1) and authority never fired once, so the knobs were measuring "
        "nothing we could use, and the thing to build was a token budget on the packet instead of a row count.")
THIN = "The porch light stays on for the cat."
RETOLD = {FAT: "The garden plan: three cedar beds along the south fence, the compost bay at the end, the neighbour's water butt, and garlic first in November; 'everything else can wait for spring'.",
          FAT2: "The knob sweep: shipped defaults won on five corpora, weight is a rank offset, authority never fired; build a token budget."}


def _core(memory, text):
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": text, "topic": "t"}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _model(answers):
    def model(prompt):
        for old, new in answers.items():
            if old[:60] in prompt:
                return json.dumps({"content": new})
        return json.dumps({"content": ""})
    return model


def test_fat_cores_without_an_episode_are_retold_fattest_first(make_hippo, memory):
    _needs_retell(memory)
    a, b, c = _core(memory, FAT), _core(memory, FAT2), _core(memory, THIN)
    h = make_hippo(model=_model(RETOLD))
    rep = h.retell(limit=5, min_tokens=20)
    assert rep["examined"] == 3 and rep["candidates"] == 2 and rep["proposed"] == 2 and rep["draft_id"], rep
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert d["terminal"] is True
    assert [op["target_core_id"] for op in d["operations"]] == [a, b], "fattest first"
    assert all(op["kind"] == "retell" for op in d["operations"])
    out = review(memory, rep["draft_id"], [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"}])
    assert [r["kind"] for r in out["results"]] == ["retell", "retell"]
    now = cores(memory)
    assert now[a]["content"] == RETOLD[FAT] and now[b]["content"] == RETOLD[FAT2] and now[c]["content"] == THIN
    sats = memory.get(f"/long/{a}/satellites").json()["satellites"]
    assert [s["content"] for s in sats] == [FAT] and sats[0]["metadata"]["was_core"] is True
    # a second pass finds nothing left to retell: the cores carry their episode now
    rep2 = make_hippo(model=_model(RETOLD)).retell(limit=5, min_tokens=20)
    assert rep2["candidates"] == 0 and rep2["proposed"] == 0 and rep2["draft_id"] is None
    assert any(r["kind"] == "retell" for r in h.state["history"])


def test_the_limit_takes_the_fattest(make_hippo, memory):
    _needs_retell(memory)
    a, b = _core(memory, FAT), _core(memory, FAT2)
    rep = make_hippo(model=_model(RETOLD)).retell(limit=1, min_tokens=20)
    assert rep["candidates"] == 2 and rep["proposed"] == 1
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert d["operations"][0]["target_core_id"] == (a if len(FAT) >= len(FAT2) else b)


def test_unchanged_longer_and_ungrounded_answers_are_skipped(make_hippo, memory):
    _needs_retell(memory)
    _core(memory, FAT)
    for answer, why in ((FAT, "unchanged"), (FAT + " And more, and more, and more.", "not_shorter"),
                        ("Zorblax visited Quendor and sold seventeen zeppelins to Marmaduke.", "ungrounded")):
        rep = make_hippo(model=lambda p, a=answer: json.dumps({"content": a})).retell(limit=5, min_tokens=20)
        assert rep["proposed"] == 0 and rep["draft_id"] is None and rep["skipped"] == {why: 1}, (why, rep)


def test_a_plain_text_answer_is_taken_and_a_cut_off_tail_is_trimmed(make_hippo, memory):
    _needs_retell(memory)
    a = _core(memory, FAT)
    answer = ("Retelling: The garden plan: three cedar beds along the south fence, years in the thinking. "
              "'Everything else can wait for spring', the note said. And then the model kept going without a full st")
    rep = make_hippo(model=lambda p: answer).retell(limit=5, min_tokens=20)
    assert rep["proposed"] == 1, rep
    op = memory.get(f"/drafts/{rep['draft_id']}").json()["operations"][0]
    assert op["target_core_id"] == a
    assert op["content"] == ("The garden plan: three cedar beds along the south fence, years in the thinking. "
                             "'Everything else can wait for spring', the note said.")


def test_named_cores_are_taken_instead_of_the_fattest(make_hippo, memory):
    _needs_retell(memory)
    a, b, c = _core(memory, FAT), _core(memory, FAT2), _core(memory, THIN)
    rep = make_hippo(model=_model(RETOLD)).retell(limit=5, min_tokens=20, core_ids=[b, c, "nope"])
    assert rep["targets"] == {"asked": 3, "candidates": 1}, "the thin one is under the bar, the unknown one is not a core"
    assert rep["proposed"] == 1
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert [op["target_core_id"] for op in d["operations"]] == [b]


def test_without_a_model_it_refuses(make_hippo, memory):
    _core(memory, FAT)
    rep = make_hippo().retell()
    assert rep["refused"] and "no model" in rep["refused"] and rep["draft_id"] is None


def test_while_a_chain_is_open_it_refuses(make_hippo, memory):
    _needs_retell(memory)
    _core(memory, FAT)
    memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": "something pending", "topic": "t"}]})
    rep = make_hippo(model=_model(RETOLD)).retell(limit=5, min_tokens=20)
    assert rep["refused"] and "chain is open" in rep["refused"]


def test_the_model_failing_is_said_not_swallowed(make_hippo, memory):
    _needs_retell(memory)
    _core(memory, FAT)

    def broken(prompt):
        raise RuntimeError("llama fell over")

    rep = make_hippo(model=broken).retell(limit=5, min_tokens=20)
    assert rep["proposed"] == 0 and rep["skipped"] == {"model_failed": 1}
    assert rep["error"] and "could not retell" in rep["error"] and rep["model_failures"]


def test_a_memory_that_predates_retell_is_refused_not_swallowed(make_hippo, memory):
    _core(memory, FAT)
    h = make_hippo(model=_model(RETOLD))
    h._mem.health = lambda: {"ok": True, "features": ["target_op"]}      # type: ignore[method-assign]
    rep = h.retell(limit=5, min_tokens=20)
    assert rep["refused"] and "predates the retell" in rep["refused"] and rep["draft_id"] is None
