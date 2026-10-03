"""
A redraft does what the critique says, and is not thrown away for it.

2 Oct 2026, the first chain to run end to end: 30 operations were denied and
only 10 came back. Seven were withdrawn, as asked. The other thirteen had
OBEYED their critiques and were dropped by this service:

- six "named a core it was not shown": the critique said 'attach to core X' or
  'attach with target_op 7', the model answered X, "7", "12" - and X was not
  among the cores it had been shown, and "7" was not a core id at all
- seven had "no usable operation": the memory was written out in the
  rationale and "content" was left out

Chad, the same morning: 'when a draft is denied, delve into the reason why...
your critiques should be heavily weighted as truthy.' Pinned here:

- the prompt says the critique is true and gives it first and last
- a core the critique names by its full id is shown, and is a valid target
- a core id may be given by its first 8+ characters
- an operation of the draft, by number: an approved one is its core; one being
  redrafted beside it becomes target_op in the new draft; if that one did not
  come back as a new core, the operation is kept as a new core of its own
- an answer with no content is asked for once more, and the second answer used
"""
from __future__ import annotations

import pytest

from conftest import as_json, review, short


def _core(memory, content: str) -> str:
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": content, "topic": "t"}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _draft(memory, h, ops: list[dict], decisions: list[dict]) -> str:
    full = [{**op, "topic": "t", "source_short_ids": [short(memory, f"fragment for op {i}", "t")]}
            for i, op in enumerate(ops)]
    did = memory.post("/drafts", json={"operations": full, "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, decisions)
    return did


def _redraft_ops(memory, t) -> list[dict]:
    return memory.get(f"/drafts/{t['resubmitted'][0]['draft_id']}").json()["operations"]


def _takes(memory, feature: str) -> bool:
    return feature in (memory.get("/health").json().get("features") or [])


def test_the_prompt_says_the_critique_is_true_and_gives_it_first_and_last(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"kind": "new_core", "content": "The model runs on port 7200.", "rationale": "as asked"})
    h = make_hippo(model=model)
    _draft(memory, h, [{"kind": "new_core", "content": "x"}],
           [{"op": 0, "verdict": "deny", "critique": "SAY THE PORT: the model runs on port 7200"}])
    h.tend()
    p = prompts[-1]
    assert "THE CRITIQUE IS TRUE" in p and "do exactly what it says" in p
    assert p.count("SAY THE PORT: the model runs on port 7200") == 2
    assert p.index("THE CRITIQUE:") < p.index("What you proposed") < p.index("The critique, once more")
    assert 'MUST HAVE "content"' in p


def test_a_core_the_critique_names_is_shown_and_is_a_valid_target(memory, make_hippo):
    wrong = _core(memory, "Chad's terms for me.")
    right = _core(memory, "Chad did 3D character rigging.")
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"kind": "attach", "target_core_id": right, "content": "Mostly self-taught 3D art.",
                        "rationale": "moved"})
    h = make_hippo(model=model)
    h._candidates = lambda entries: [{"id": wrong, "content": "Chad's terms for me.", "topic": "t"}]   # type: ignore
    _draft(memory, h, [{"kind": "attach", "content": "x", "target_core_id": wrong}],
           [{"op": 0, "verdict": "deny", "critique": f"Wrong target. Attach to core {right} (the rigging core)."}])
    t = h.tend()
    assert t["model_failures"] == [], "naming the core the reviewer named is not inventing one"
    assert f"({right})" in prompts[-1] and "Chad did 3D character rigging." in prompts[-1]
    op = _redraft_ops(memory, t)[0]
    assert (op["kind"], op["target_core_id"], op["content"]) == ("attach", right, "Mostly self-taught 3D art.")


def test_a_core_id_may_be_given_by_its_first_characters(memory, make_hippo):
    right = _core(memory, "The model core.")
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": right[:8], "content": "An episode."}))
    h._candidates = lambda entries: [{"id": right, "content": "The model core.", "topic": "t"}]   # type: ignore
    _draft(memory, h, [{"kind": "new_core", "content": "x"}],
           [{"op": 0, "verdict": "deny", "critique": f"make it an attach to core {right[:8]}"}])
    assert _redraft_ops(memory, h.tend())[0]["target_core_id"] == right


def test_an_approved_operation_named_by_number_is_its_core(memory, make_hippo):
    """'Attach with target_op 0', and the model answers target_core_id "0"."""
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"kind": "attach", "target_core_id": "0", "content": "Playtested time and time again."})
    h = make_hippo(model=model)
    h._candidates = lambda entries: []                                           # type: ignore
    did = _draft(memory, h, [{"kind": "new_core", "content": "Super Dude Bros is Chad's board game."},
                             {"kind": "new_core", "content": "x"}],
                 [{"op": 0, "verdict": "approve"},
                  {"op": 1, "verdict": "deny", "critique": "Make this an attach with target_op 0 (the Super Dude Bros core)."}])
    made = memory.get(f"/drafts/{did}").json()["operations"][0]["long_term_id"]
    t = h.tend()
    assert f"op 0: approved - it is core ({made}) now" in prompts[-1]
    op = _redraft_ops(memory, t)[0]
    assert (op["kind"], op["target_core_id"]) == ("attach", made)


def test_an_operation_being_redrafted_beside_it_becomes_target_op(memory, make_hippo):
    if not _takes(memory, "target_op"):
        pytest.skip("the installed seren-memory predates target_op")

    def model(p):
        if "What you proposed (operation 1)" in p:
            assert "op 0: denied too and being redrafted now" in p
            return as_json({"kind": "attach", "target_op": 0, "content": "The Boss expansion is half baked."})
        return as_json({"kind": "new_core", "content": "Super Dude Bros is Chad's dice-race board game."})
    h = make_hippo(model=model)
    h._candidates = lambda entries: []                                           # type: ignore
    _draft(memory, h, [{"kind": "new_core", "content": "a game"}, {"kind": "new_core", "content": "x"}],
           [{"op": 0, "verdict": "deny", "critique": "say which game and what kind"},
            {"op": 1, "verdict": "deny", "critique": "Attach with target_op 0, the Super Dude Bros core op 0 should become."}])
    ops = _redraft_ops(memory, h.tend())
    assert [o["kind"] for o in ops] == ["new_core", "attach"]
    assert ops[1]["target_op"] == 0 and ops[1]["target_core_id"] is None and "_wants_op" not in ops[1]
    assert ops[1]["content"] == "The Boss expansion is half baked."


def test_if_that_operation_did_not_come_back_as_a_new_core_this_one_is_kept_as_one(memory, make_hippo):
    said: list[str] = []

    def model(p):
        if "What you proposed (operation 1)" in p:
            return as_json({"kind": "attach", "target_core_id": "0", "content": "The Boss expansion is half baked."})
        return as_json({"withdraw": True, "rationale": "a duplicate"})
    h = make_hippo(model=model)
    h._log = said.append
    h._candidates = lambda entries: []                                           # type: ignore
    _draft(memory, h, [{"kind": "new_core", "content": "a game"}, {"kind": "new_core", "content": "x"}],
           [{"op": 0, "verdict": "deny", "critique": "withdraw, a duplicate"},
            {"op": 1, "verdict": "deny", "critique": "attach with target_op 0"}])
    ops = _redraft_ops(memory, h.tend())
    assert len(ops) == 1 and ops[0]["kind"] == "new_core" and ops[0]["target_core_id"] is None
    assert ops[0]["content"] == "The Boss expansion is half baked.", "kept, for the reviewer to place"
    assert any("did not come back as a new core" in m for m in said)


def test_an_answer_with_no_content_is_asked_for_once_more(memory, make_hippo):
    """The memory written into the rationale, and "content" left out."""
    core = _core(memory, "The model core.")
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        if len(prompts) == 1:
            return as_json({"kind": "attach", "target_core_id": core,
                            "rationale": "26 Sept 2026: the first chain finished on the new model."})
        return as_json({"kind": "attach", "target_core_id": core,
                        "content": "26 Sept 2026: the first chain finished on the new model.", "rationale": "dated"})
    h = make_hippo(model=model)
    h._candidates = lambda entries: [{"id": core, "content": "The model core.", "topic": "t"}]   # type: ignore
    _draft(memory, h, [{"kind": "new_core", "content": "x"}],
           [{"op": 0, "verdict": "deny", "critique": "make it a dated satellite"}])
    t = h.tend()
    assert len(prompts) == 2 and 'has no "content"' in prompts[1] and prompts[1].startswith(prompts[0])
    assert _redraft_ops(memory, t)[0]["content"] == "26 Sept 2026: the first chain finished on the new model."


def test_asked_twice_and_still_nothing_it_is_dropped_and_said(memory, make_hippo):
    said: list[str] = []
    calls: list[str] = []

    def model(p):
        calls.append(p)
        return as_json({"kind": "new_core", "rationale": "the memory, in the wrong place"})
    h = make_hippo(model=model)
    h._log = said.append
    _draft(memory, h, [{"kind": "new_core", "content": "x"}], [{"op": 0, "verdict": "deny", "critique": "say more"}])
    t = h.tend()
    assert len(calls) == 2 and t["resubmitted"] == []
    assert any("no usable operation" in m for m in said)


def test_a_made_up_core_is_still_refused(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": "f" * 32, "content": "x"}))
    h._candidates = lambda entries: []                                           # type: ignore
    _draft(memory, h, [{"kind": "new_core", "content": "x"}], [{"op": 0, "verdict": "deny", "critique": "attach it somewhere"}])
    t = h.tend()
    assert t["resubmitted"] == [] and "not shown" in t["model_failures"][0]["why"]
