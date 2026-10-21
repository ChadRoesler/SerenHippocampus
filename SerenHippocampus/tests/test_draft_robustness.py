"""
What the first sleep that ran on its own showed (30 Sept 2026): seven of
eleven answers were cut off at model.max_tokens and lost whole, and seventeen
of the eighteen operations that did arrive were denied.

Pinned here:
- an answer cut off mid-operation keeps the operations it finished, the sleep
  report counts it (cut_off), and it is not a model failure
- an answer cut off before ONE operation finished is a failure that names
  model.max_tokens, not 'not the JSON asked for'
- a pile yields at most its fragments + 1 operations, and the prompt names no
  number (a number became a target)
- the voice card is scoped to fragments about its owner
- an operation citing every fragment keeps only the ones its text came from
- a restated_content that just repeats content is dropped (approving it would
  overwrite the core's text with the episode)
- the prompt asks for few operations and honest citations
- the voice block says the card is how to write, not what
- a woken run is told nobody is at the keyboard; an operator's own message is
  left as written
- a yaml key the config does not have is named, with where it likely belongs
"""
from __future__ import annotations

import json

from conftest import as_json, short
from seren_hippocampus.config import RippleConfig, load_config, warn_unknown_settings
from seren_hippocampus.ripple import WOKEN, Ripple
from seren_hippocampus.sleep import cited_fragments, salvage_operations

OP_A = {"kind": "new_core", "content": "The promise, as the user made it.", "rationale": "x", "source_indexes": [0]}
OP_B = {"kind": "new_core", "content": "The NUC moved to jammy.", "rationale": "x", "source_indexes": [1]}


def _cut(ops: list[dict], tail: str) -> str:
    """An answer whose last operation ran out of tokens."""
    return '{"operations": [' + ", ".join(json.dumps(o) for o in ops) + (", " if ops else "") + tail


def test_salvage_reads_the_finished_operations():
    ops, cut = salvage_operations(_cut([OP_A, OP_B], '{"kind": "attach", "content": "and then it ran ou'))
    assert [o["content"] for o in ops] == [OP_A["content"], OP_B["content"]] and cut is True
    ops, cut = salvage_operations(as_json({"operations": [OP_A]}))
    assert len(ops) == 1 and cut is False
    assert salvage_operations("no json here at all") == ([], False)
    assert salvage_operations(_cut([], '{"kind": "new_core", "content": "cut straight awa')) == ([], True)


def test_a_cut_off_answer_keeps_what_it_finished(make_hippo, memory):
    h = make_hippo(model=lambda p: _cut([OP_A, OP_B], '{"kind": "new_core", "content": "cut off he'),
                   promote_min_evidence=1)
    short(memory, "the user's promise", "t"); short(memory, "the NUC moved to jammy", "t")
    rep = h.sleep()
    assert rep["operations"] == 2 and rep["cut_off"] == 1
    assert rep["model_failures"] == [], "two finished operations are not a failure"


def test_cut_off_before_one_operation_names_the_cap(make_hippo, memory):
    h = make_hippo(model=lambda p: _cut([], '{"kind": "new_core", "content": "it never got to the en'),
                   promote_min_evidence=1)
    short(memory, "a", "t"); short(memory, "b", "t")
    rep = h.sleep()
    why = rep["model_failures"][0]["why"]
    assert "model.max_tokens (2000)" in why and "cut off" in why and "not the JSON" not in why


def test_a_pile_yields_a_few_operations_not_one_per_sentence(make_hippo, memory):
    many = [{"kind": "new_core", "content": f"statement number {i}", "rationale": "x", "source_indexes": [0]}
            for i in range(6)]
    h = make_hippo(model=lambda p: as_json({"operations": many}), promote_min_evidence=1)
    short(memory, "one memory, six sentences", "t")
    assert h.sleep()["operations"] == 2, "its one fragment, plus one"


def test_an_operation_cites_only_the_fragments_it_came_from():
    entries = [{"id": "crawl", "content": "The user's knight died of a dragon flashback in Dungeon Crawl, armour phobia and all"},
               {"id": "nuc", "content": "the NUC moved to jammy and the port changed to 7267"},
               {"id": "crawl2", "content": "the knight threw off his plate armour: armour phobia, in Dungeon Crawl"}]
    text = "In Dungeon Crawl, the user's knight got armour phobia, threw off his plate armour, and died of a dragon flashback."
    assert cited_fragments(text, [0, 1, 2], entries) == [0, 2], "the NUC fragment is not what this says"
    assert cited_fragments(text, [1], entries) == [1], "one citation is left alone"
    assert cited_fragments("", [0, 1], entries) == [0, 1], "nothing to compare: left alone"


def test_an_operation_not_drawn_from_the_fragments_is_dropped(make_hippo, memory):
    """The real 4B, 30 Sept 2026: a Dungeon Crawl pile came back with a
    'verbatim' operation that was the dream CORE's text, copied from the cores
    it was shown. No fragment in the pile says it."""
    from seren_hippocampus.sleep import grounded
    crawl = ("The user's first crawl story: a new campaign with horrible luck, they barely survived the opening Red Dragon "
         "fight, the knight wore the full plate set, drew Dragon in heat, rolled Armourphobia and threw off his plate")
    dream = ("In the user's dream, the assistant is a gardener, tall, a green scarf and muddy boots, who greets with "
             "a wave and a cup of tea")
    own = "The user's first crawl campaign had horrible luck: the knight in the full plate set rolled Armourphobia against the Red Dragon."
    entries = [{"id": "k", "content": crawl}]
    assert grounded(own, entries) is True and grounded(dream, entries) is False
    assert grounded("x", entries) is True and grounded(dream, [{"id": "s", "content": "a"}]) is True, "too short to judge"
    h = make_hippo(model=lambda p: as_json({"operations": [
        {"kind": "new_core", "content": own, "rationale": "x", "source_indexes": [0]},
        {"kind": "verbatim", "content": dream, "rationale": "x", "source_indexes": [0]}]}), promote_min_evidence=1)
    said: list[str] = []
    h._log = said.append
    short(memory, crawl, "alice,crawl")
    did = h.sleep()["draft_id"]
    ops = memory.get(f"/drafts/{did}").json()["operations"]
    assert [op["kind"] for op in ops] == ["new_core"] and "Armourphobia" in ops[0]["content"]
    assert any("not in the fragments" in m for m in said)


def test_a_restatement_that_repeats_the_content_is_dropped(make_hippo, memory):
    core = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": "A dream the user described.", "topic": "t"}]}).json()["id"]
    from conftest import review
    cid = review(memory, core, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    same = "In the user's dream, the assistant has a green scarf."
    h = make_hippo(model=lambda p: as_json({"operations": [
        {"kind": "attach", "target_core_id": cid, "content": same, "restated_content": same,
         "rationale": "x", "source_indexes": [0]}]}), promote_min_evidence=1)
    h._candidates = lambda entries: [{"id": cid, "content": "A dream the user described.", "topic": "t"}]   # type: ignore[method-assign]
    short(memory, "the dream: a green scarf in the rain", "t")
    did = h.sleep()["draft_id"]
    op = memory.get(f"/drafts/{did}").json()["operations"][0]
    assert op["kind"] == "attach" and not op.get("restated_content")


def test_the_prompts_ask_for_few_operations_and_honest_citations(make_hippo, memory):
    prompts: list[str] = []
    h = make_hippo(model=lambda p: prompts.append(p) or as_json({"operations": []}), promote_min_evidence=1)
    h._cfg.voice.enabled = True
    h.voice.set("I'm the assistant, they/them.")
    short(memory, "a", "t")
    h.sleep()
    p = prompts[0]
    assert "never more operations than fragments" in p and "ONLY the fragments" in p and "never repeat content" in p
    # No number: 'at most three' made the real 4B write exactly three, every time.
    assert "three operations" not in p.lower()
    # The card is scoped to fragments about its owner, and is never content.
    assert "ONLY where a fragment is about them" in p and "never as 'I'" in p
    assert "HOW to write, never what" in p


def test_a_woken_run_is_told_nobody_is_there():
    r = Ripple(RippleConfig(type="script"), log=lambda m: None)
    for kind in ("brief_requested", "draft_submitted", "tend_resubmitted", "brief_unmatched"):
        assert r.message({"event": kind, "draft_id": "d1"}).startswith(WOKEN), kind
    assert "nobody is at the keyboard" in WOKEN
    mine = Ripple(RippleConfig(type="script", messages={"brief_requested": "Bedtime, friend."}), log=lambda m: None)
    assert mine.message({"event": "brief_requested"}) == "Bedtime, friend.", "an operator's own wording is left alone"


def test_a_setting_in_the_wrong_block_is_named(tmp_path, capsys):
    p = tmp_path / "seren-hippocampus.yaml"
    p.write_text("model:\n  url: http://127.0.0.1:7200/v1\n  lifecycle:\n    keep_warm_seconds: 300\n"
                 "    max_tokens: 4096\nsleep:\n  bedtime: never\n", encoding="utf-8")
    cfg = load_config(str(p))
    out = capsys.readouterr().out
    assert "'model.lifecycle.max_tokens' is not a setting and was ignored - did you mean model.max_tokens?" in out
    assert "'sleep.bedtime' is not a setting and was ignored" in out
    assert cfg.model.max_tokens == 2000, "the misplaced value was not applied, and now says so"
    assert warn_unknown_settings({"model": {"max_tokens": 4096}, "voice": {"enabled": True}}) == []
