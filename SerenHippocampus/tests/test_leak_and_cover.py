"""
Two things the 3 Oct 2026 sleep showed.

THE LEAK. A redraft put the prompt's own scaffolding - "Source fragments (the
critique may name them by the id in brackets):" and the list under it -
inside its content, and it was approved on the last attempt with the reviewer
cutting it out by hand. A memory is never the prompt echoed back.

THE LOSS. An operation that cited a two-subject fragment (a backup design,
and an unrelated second subject) landed holding only the first; approving it
archived the fragment, and the second subject was gone. An operation now carries only the
fragments it covers; the rest stay in short-term for a later sleep.
"""
from __future__ import annotations

from conftest import as_json, review, short

from seren_hippocampus.sleep import covered_fragments, strip_scaffolding

TWO_SUBJECTS = ("The user's mental model for backing up the brain's stores: Sinew holds the backup mechanism so Lodestar can "
        "later request, pull and stash backups; a configurable full backup plus daily snapshots; it must cope with "
        "an embedder change. They also described the garden: a long table, a lemon tree, a gate that sticks in the "
        "rain.")
MODEL_ONLY = ("The user's mental model for backing up the stores: Sinew holds the backup mechanism so Lodestar "
              "can later request, pull and stash backups; a configurable full backup plus daily snapshots; it must "
              "cope with an embedder change.")


def test_scaffolding_is_cut_from_a_memory():
    text = ("The user gave the assistant standing permission to change the live brain.\n\n"
            "Source fragments (the critique may name them by the id in brackets):\n- [a8938f8f] 2 Oct 2026...")
    assert strip_scaffolding(text) == ("The user gave the assistant standing permission to change the live brain.", True)
    assert strip_scaffolding("Existing cores:\n(abc) ...") == ("", True)
    assert strip_scaffolding("A plain memory, nothing cut.") == ("A plain memory, nothing cut.", False)
    assert strip_scaffolding("He said 'the critique was fair'.") == ("He said 'the critique was fair'.", False), \
        "only a LINE that starts as scaffolding is cut"


def test_a_redraft_that_echoes_the_prompt_lands_without_it(memory, make_hippo):
    said: list[str] = []
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "content":
        "The user gave the assistant standing permission to change the live brain.\n\nSource fragments (the critique may name them):\n- [x] ..."}))
    h._log = said.append
    a = short(memory, "The user gave the assistant standing permission to change the live brain", "t")
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": "x", "topic": "t", "source_short_ids": [a]}],
                                       "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "say it properly"}])
    t = h.tend()
    op = memory.get(f"/drafts/{t['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["content"] == "The user gave the assistant standing permission to change the live brain."
    assert any("cut prompt scaffolding" in m for m in said)


def test_a_draft_that_echoes_the_prompt_lands_without_it(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json({"operations": [{"kind": "new_core", "source_indexes": [0],
        "content": "The model runs on port 7200.\nFragments:\n[0] the model runs on port 7200"}]}), promote_min_evidence=1)
    short(memory, "the model runs on port 7200", "t")
    op = memory.get(f"/drafts/{h.sleep()['draft_id']}").json()["operations"][0]
    assert op["content"] == "The model runs on port 7200."


def test_an_operation_carries_only_the_fragments_it_covers():
    entries = [{"id": "two", "content": TWO_SUBJECTS}, {"id": "short", "content": "a short line, a small note after"}]
    carried, left = covered_fragments(MODEL_ONLY, [0, 1], entries)
    assert left == [0], "a fragment with a sentence the operation says nothing of is not carried"
    assert carried == [1], "a short one is carried"
    assert covered_fragments(TWO_SUBJECTS, [0], entries) == ([0], [])
    reworded = ("The user's backup model: the mechanism lives in Sinew, Lodestar requests and stashes "
                "backups later, full backups on a schedule plus daily snapshots, and an embedder change is coped "
                "with. And they described the garden: the long table, the lemon tree, the gate that sticks in the rain.")
    assert covered_fragments(reworded, [0], entries) == ([0], []), "a paraphrase that carries both subjects is carried"


def test_the_second_subject_is_not_archived_by_the_backup_core(memory, make_hippo):
    """The loss itself, at draft time: the operation lands, the fragment stays."""
    h = make_hippo(model=lambda p: as_json({"operations": [{"kind": "new_core", "content": MODEL_ONLY, "source_indexes": [0, 1]}]}),
                   promote_min_evidence=1)
    said: list[str] = []
    h._log = said.append
    two_subjects = short(memory, TWO_SUBJECTS, "backup")
    other = short(memory, "The user's model: Sinew holds the backup mechanism, Lodestar pulls later.", "backup")
    did = h.sleep()["draft_id"]
    op = memory.get(f"/drafts/{did}").json()["operations"][0]
    assert two_subjects not in op["source_short_ids"] and other in op["source_short_ids"]
    assert any("carries less than half" in m for m in said)
    review(memory, did, [{"op": 0, "verdict": "approve"}])
    ids = {e["id"] for e in memory.get("/short", params={"limit": 50}).json()["entries"]}
    assert two_subjects in ids and other not in ids, "the second subject stays for a later sleep; what was carried is archived"


def test_a_redraft_carries_only_what_it_covers_too(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json({"kind": "new_core", "content": MODEL_ONLY}))
    two_subjects = short(memory, TWO_SUBJECTS, "t")
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": "x", "topic": "t", "source_short_ids": [two_subjects]}],
                                       "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "say the model"}])
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["source_short_ids"] == [] and op["evidence_count"] == 1 and op["content"] == MODEL_ONLY
