"""
Grouping memories and matching a brief's hints (30 Sept 2026: hip-hint-match,
hip-cluster-tags, hip-brief-unmatched - all three hit live on 28 Sept).

Pinned here:
- a hint matches by its exact text, by being one of the memories' tags, or -
  promote hints only - by most of its words; a sentence written ABOUT a memory
  ("the garden's failing tell: losing the beds") now finds the memory, whose
  own words are different. Noise hints stay strict.
- memories group by shared tags, so three copies of one dream tagged three
  ways are one pile of three - which is evidence - and a pile cannot chain
  into an unrelated one
- a brief whose promote hints matched nothing says so (the report lists each
  hint, the brief_unmatched event goes out, the ripple asks for a rewrite), and
  when nothing was drafted it is parked, not consumed: the check skips it
  instead of sleeping on it every tick, and a new brief supersedes it
"""
from __future__ import annotations

from conftest import short
from seren_hippocampus.config import RippleConfig
from seren_hippocampus.sleep import CLUSTER_MAX, _tags, _words, cluster_by_tags, hint_match

# A memory, and (below) hints written about it in other words - the shape of
# the live miss on 28 Sept: the hint was a sentence about the memory, the
# memory said the same thing differently, and letter-for-letter found nothing.
PASSAGE = ("The tell that the garden was failing: the beds stopped draining after rain. Puddles stood where "
           "the paths used to dry by noon. The story of a season that drowned.")


def _m(haystack: str, hint: str, tags=frozenset(), loose=True):
    return hint_match(hint, frozenset(tags), haystack.lower(), _words(haystack), loose=loose)


def test_a_hint_matches_exactly_by_tag_or_by_its_words():
    assert _m(PASSAGE, "stopped draining") == "exact"
    assert _m(PASSAGE, "dream", tags={"alice", "dream"}) == "tag"
    assert _m(PASSAGE, "the garden's failing tell: losing the beds") == "words", "a sentence about the memory, in other words"
    assert _m(PASSAGE, "the harvest festival and the archives") is None
    assert _m(PASSAGE, "harvest") is None, "one word matches exactly or not at all"


def test_noise_hints_stay_strict():
    assert _m(PASSAGE, "the garden's failing tell: losing the beds", loose=False) is None
    assert _m(PASSAGE, "stopped draining", loose=False) == "exact"


def _s(topic, i=0):
    return {"id": f"{topic}-{i}", "content": "x", "metadata": {"topic": topic}}


def test_one_story_tagged_three_ways_is_one_pile():
    piles = cluster_by_tags([_s("alice,bob,identity"), _s("alice,bob,identity,dream"),
                             _s("alice,bob,dream,identity"), _s("hippocampus,ripple,bug")])
    sizes = sorted(len(m) for _, m in piles)
    assert sizes == [1, 3]
    assert piles[0][0] == "alice,bob,identity", "a pile keeps its first memory's topic"


def test_a_pile_does_not_chain_and_is_capped():
    piles = cluster_by_tags([_s("a,b,c"), _s("a,b,c,d"), _s("c,d,e")])
    assert [len(m) for _, m in piles] == [2, 1], "c,d,e is near a,b,c,d but not the pile's first memory"
    many = cluster_by_tags([_s("t", i) for i in range(CLUSTER_MAX + 3)])
    assert [len(m) for _, m in many] == [CLUSTER_MAX, 3]
    untagged = cluster_by_tags([{"id": "u", "content": "x", "metadata": {}}])
    assert untagged == [("_untagged", untagged[0][1])]
    assert _tags(" Alice , bob,, ") == frozenset({"alice", "bob"})


def _brief(memory, promote, noise=()):
    return memory.post("/brief", json={"summary": "what mattered", "promote_hints": list(promote),
                                       "noise_hints": list(noise)}).json()["id"]


def test_a_sentence_hint_keeps_a_lone_memory(make_hippo, memory):
    """Mechanical mode, promote_min_evidence 2: one memory is kept only if a
    hint names it - and now a natural sentence can."""
    short(memory, PASSAGE, "garden,history")
    _brief(memory, ["the garden's failing tell: losing the beds"])
    h = make_hippo()
    rep = h.sleep()
    assert rep["operations"] == 1 and rep["hints"]["promote"]["the garden's failing tell: losing the beds"] == ["garden,history"]
    assert "brief_unmatched" not in rep


def test_three_copies_of_one_dream_are_evidence(make_hippo, memory):
    for i, topic in enumerate(("alice,bob,identity", "alice,bob,identity,dream", "alice,bob,dream,identity")):
        short(memory, f"A dream the user described, copy {i}: a red door", topic)
    rep = make_hippo().sleep()
    assert rep["clusters"] == 1 and rep["operations"] == 1, "one pile of three, not three piles of one"


def test_an_unmatched_brief_is_said_and_parked_not_consumed(make_hippo, memory):
    short(memory, "node-a moved to jammy", "nodes")
    bid = _brief(memory, ["the harvest festival"])
    h = make_hippo()
    rep = h.sleep()
    assert rep["brief_unmatched"] is True and rep["hints"]["promote"] == {"the harvest festival": []}
    assert rep["operations"] == 0 and rep["draft_id"] is None
    ev = [e for e in h.state["events"] if e["event"] == "brief_unmatched"]
    assert ev and ev[-1]["brief_id"] == bid and ev[-1]["hints"] == ["the harvest festival"]
    assert h._mem.latest_brief()["id"] == bid, "not consumed"
    assert bid in h.state["parked_briefs"]
    chk = h.check()
    assert chk["status"] == "brief_unmatched" and chk["sleep"] is None, "parked: the tick does not sleep on it again"
    new = _brief(memory, ["jammy"])
    chk = h.check()
    assert chk["brief_id"] == new and chk["status"] == "sleeping", "a rewritten brief supersedes the parked one"


def test_the_ripple_asks_for_a_rewrite_by_default():
    assert "brief_unmatched" in RippleConfig().events
