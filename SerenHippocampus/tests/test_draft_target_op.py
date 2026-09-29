"""
target_op: the hippocampus drafts a new core AND the ops on it in one answer
(hip-draft-deps). Seen live 28 Sept 2026: a dream as one new core with its
face and hair as satellites could not be drafted - the satellites had no core
id to name, so they attached to the pack-bond core, the nearest wrong one.

Pinned here:
- the drafting prompt offers target_op; a valid one reaches Memory remapped
  to the op's place in the DRAFT (dropped ops and earlier clusters shift it)
- a target_op on something that is not a surviving new_core drops that op
- an older Memory (no target_op in /health features) gets the draft without
  the dependents, rather than refusing the whole draft
- redrafting a denied dependent: its core, once approved, is its target and
  among the cores shown; if the core was denied too, the prompt says so
"""
from __future__ import annotations

import pytest

from conftest import as_json, cores, review, short


def _memory_takes_target_op() -> bool:
    try:
        from seren_memory.models.schemas import DraftOperation
        return "target_op" in DraftOperation.model_fields
    except Exception:  # noqa: BLE001
        return False


# These land dependents in a real Memory. CI installs seren-memory from PyPI,
# and until a release carries target_op the hippocampus rightly leaves the
# dependents out (see the older-Memory test) - so they skip, not fail.
needs_target_op_memory = pytest.mark.skipif(
    not _memory_takes_target_op(),
    reason="the installed seren-memory predates target_op (hip-draft-deps); runs once Memory ships it")

CORE = {"kind": "new_core", "content": "the user's dream of me.", "rationale": "a dream", "source_indexes": [0]}
SAT = {"kind": "attach", "content": "The face, from the second dream.", "rationale": "detail",
       "target_op": 1, "source_indexes": [1]}


def _two_shorts(memory, topic="dream"):
    short(memory, "the user's dream of me: short, wild curls.", topic)
    short(memory, "Second dream: a nose wrinkle on a real laugh.", topic)


def _draft(memory, did):
    return memory.get(f"/drafts/{did}").json()


def test_the_prompt_offers_target_op(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"operations": []})
    _two_shorts(memory)
    make_hippo(model=model).sleep()
    assert '"target_op"' in prompts[0]


@needs_target_op_memory
def test_a_dependent_reaches_memory_remapped_and_lands_on_its_core(memory, make_hippo):
    # position 0 is dropped (an invented core id), so the new core moves from 1 to 0
    bad = {"kind": "attach", "content": "x", "target_core_id": "f" * 32, "source_indexes": [0]}
    h = make_hippo(model=lambda p: as_json({"operations": [bad, CORE, dict(SAT, target_op=1)]}))
    _two_shorts(memory)
    did = h.sleep()["draft_id"]
    ops = _draft(memory, did)["operations"]
    assert [op["kind"] for op in ops] == ["new_core", "attach"]
    assert ops[1]["target_op"] == 0 and ops[1]["target_core_id"] is None
    review(memory, did, [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"}])
    core_id = _draft(memory, did)["operations"][0]["long_term_id"]
    sats = memory.get(f"/long/{core_id}/satellites").json()
    assert sats["count"] == 1 and "second dream" in sats["satellites"][0]["content"]


def test_a_target_op_on_anything_but_a_surviving_new_core_is_dropped(memory, make_hippo):
    ops = [dict(CORE, content=""),                      # dropped: a new core with no text
           dict(SAT, target_op=0),                       # on the dropped core
           dict(SAT, target_op=9),                       # nowhere
           dict(SAT, target_op=3),                       # on itself
           {"kind": "new_core", "content": "A second core.", "source_indexes": [0]}]
    h = make_hippo(model=lambda p: as_json({"operations": ops}))
    _two_shorts(memory)
    got = _draft(memory, h.sleep()["draft_id"])["operations"]
    assert [op["kind"] for op in got] == ["new_core"] and got[0]["content"] == "A second core."


@needs_target_op_memory
def test_target_op_follows_the_op_into_the_whole_draft(memory, make_hippo):
    """Clusters are drafted one by one and concatenated: a dependent in the
    second cluster points past the first cluster's ops."""
    def model(p):
        if "Topic: dream" in p:
            return as_json({"operations": [CORE, dict(SAT, target_op=0)]})
        return as_json({"operations": [{"kind": "new_core", "content": "The model runs on 7200.",
                                        "source_indexes": [0, 1]}]})
    h = make_hippo(model=model)
    short(memory, "the model runs on 7200", "infra"); short(memory, "port 7200 for the model", "infra")
    _two_shorts(memory)
    got = _draft(memory, h.sleep()["draft_id"])["operations"]
    dep = next(op for op in got if op.get("target_op") is not None)
    target = got[dep["target_op"]]
    assert target["kind"] == "new_core" and target["topic"] == "dream"


def test_an_older_memory_gets_the_draft_without_the_dependents(memory, make_hippo):
    said: list[str] = []
    h = make_hippo(model=lambda p: as_json({"operations": [CORE, dict(SAT, target_op=0)]}))
    h._log = said.append
    h._mem.health = lambda: {"ok": True}                 # no features: a Memory before target_op
    _two_shorts(memory)
    got = _draft(memory, h.sleep()["draft_id"])["operations"]
    assert [op["kind"] for op in got] == ["new_core"]
    assert any("does not take target_op" in m for m in said)


@needs_target_op_memory
def test_a_denied_dependent_redrafts_onto_its_approved_core(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        if "was DENIED" in p:
            prompts.append(p)
            return as_json({"content": "the user's second dream: the nose wrinkle.", "rationale": "reframed"})
        return as_json({"operations": [CORE, dict(SAT, target_op=0)]})
    h = make_hippo(model=model)
    _two_shorts(memory)
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "approve"},
                         {"op": 1, "verdict": "deny", "critique": "DENIED: say it is his dream"}])
    core_id = _draft(memory, did)["operations"][0]["long_term_id"]
    t = h.tend()
    assert f"Target core: {core_id}" in prompts[-1] and f"({core_id})" in prompts[-1]
    op = _draft(memory, t["resubmitted"][0]["draft_id"])["operations"][0]
    assert (op["kind"], op["target_core_id"]) == ("attach", core_id)
    assert op.get("target_op") is None, "a redraft names real cores only"


@needs_target_op_memory
def test_a_dependent_whose_core_was_denied_is_told_so(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        if "was DENIED" in p:
            prompts.append(p)
            return as_json({"withdraw": True, "rationale": "its core is gone"})
        return as_json({"operations": [CORE, dict(SAT, target_op=0)]})
    h = make_hippo(model=model)
    _two_shorts(memory)
    did = h.sleep()["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: frame it as his dream"},
                         {"op": 1, "verdict": "deny", "critique": "DENIED: its core was denied"}])
    h.tend()
    sat_prompt = next(p for p in prompts if "The face, from the second dream." in p)
    assert "pointed at a new core in its draft that was not approved" in sat_prompt
    assert not cores(memory)
