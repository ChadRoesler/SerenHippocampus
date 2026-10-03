"""
Which existing cores the drafting model is shown, and how much of each.

2 Oct 2026, from the first chain that ran end to end: of 30 first-round
denials, about nine were aimed at the wrong core and six repeated a core that
already said it. The model was shown five cores at 300 characters each, found
by one search on every fragment glued together - and Memory's search threw
satellites away AFTER fetching, so a well-recorded subject's own episodes
crowded its core out. Pinned here:

- a satellite hit counts for its core, and the core is shown even when no
  search returned the core itself
- one search per fragment: a pile of two subjects finds both cores
- superseded cores are not offered
- the model sees the topic, core_chars of the text, the episode count, and the
  episode that led there
- a Memory that cannot be searched means no cores, said in the log
"""
from __future__ import annotations

from conftest import as_json, review, short


def _core(memory, content: str, topic: str = "t") -> str:
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": content, "topic": topic}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _attach(memory, core: str, content: str) -> None:
    did = memory.post("/drafts", json={"operations": [
        {"kind": "attach", "content": content, "topic": "t", "target_core_id": core}]}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "approve"}])


def test_a_satellite_hit_brings_its_core(memory, make_hippo):
    h = make_hippo()
    core = _core(memory, "Chad makes board games.", "games")
    sat = "Super Dude Bros is a push-your-luck dice race styled as an 8-bit platformer."
    _attach(memory, core, sat)
    hits = [{"id": "s1", "tier": "long", "content": sat, "metadata": {"kind": "satellite", "core_id": core}}]
    h._mem.search_long = lambda q, n=5: hits                 # only the satellite came back, as it did live
    got = h._candidates([{"content": "Super Dude Bros has a boss expansion."}])
    assert [c["id"] for c in got] == [core]
    assert got[0]["content"] == "Chad makes board games." and got[0]["topic"] == "games"
    assert got[0]["satellites"] == 1 and got[0]["via"] == sat


def test_the_real_search_finds_a_core_through_its_satellites(memory, make_hippo):
    """Against a real Memory: the client asks for satellites, so they are
    not dropped before the hippocampus can follow them home."""
    h = make_hippo()
    core = _core(memory, "Chad makes board games.")
    _attach(memory, core, "Super Dude Bros is a dice race with cheat codes.")
    rows = h._mem.search_long("Super Dude Bros is a dice race with cheat codes.", n=5)
    assert any((r.get("metadata") or {}).get("kind") == "satellite" for r in rows)
    assert core in [c["id"] for c in h._candidates([{"content": "Super Dude Bros is a dice race with cheat codes."}])]


def test_one_search_per_fragment_finds_both_subjects(memory, make_hippo):
    h = make_hippo()
    asked: list[str] = []

    def search(q, n=5):
        asked.append(q)
        if q == "the feet story":
            return [{"id": "kdm", "tier": "long", "content": "KDM stories.", "metadata": {"kind": "core", "topic": "kdm"}}]
        if q == "the nudge":
            return [{"id": "hip", "tier": "long", "content": "The nudge.", "metadata": {"kind": "core"}}]
        return []                                            # the two glued together are near neither
    h._mem.search_long = search
    got = h._candidates([{"content": "the feet story"}, {"content": "the nudge"}])
    assert {c["id"] for c in got} == {"kdm", "hip"}
    assert asked == ["the feet story", "the nudge", "the feet story the nudge"]


def test_what_is_found_most_often_and_highest_comes_first_and_the_list_is_capped(memory, make_hippo):
    h = make_hippo(candidate_cores=2)
    rows = lambda *ids: [{"id": i, "tier": "long", "content": i, "metadata": {"kind": "core"}} for i in ids]
    answers = {"a": rows("x", "y"), "b": rows("y", "z"), "a b": rows("y")}
    h._mem.search_long = lambda q, n=5: answers[q]
    assert [c["id"] for c in h._candidates([{"content": "a"}, {"content": "b"}])] == ["y", "x"]


def test_superseded_cores_are_not_offered(memory, make_hippo):
    h = make_hippo()
    h._mem.search_long = lambda q, n=5: [
        {"id": "old", "tier": "long", "content": "blue", "metadata": {"kind": "core", "superseded_by": "new"}},
        {"id": "s", "tier": "long", "content": "ep", "metadata": {"kind": "satellite", "core_id": "gone"}}]
    h._mem.core = lambda cid: {"id": cid, "content": "x", "metadata": {"superseded_by": "new"}}
    assert h._candidates([{"content": "favourite colour"}]) == []


def test_the_model_reads_enough_of_each_core(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"operations": []})
    h = make_hippo(model=model, core_chars=120)
    long_text = "The model core. " + "x" * 400
    h._candidates = lambda entries: [                        # type: ignore[method-assign]
        {"id": "c1", "content": long_text, "topic": "model", "satellites": 3, "via": "attempt 3 ran on the 4B"},
        {"id": "c2", "content": "Short.", "topic": None}]
    short(memory, "a", "t"); short(memory, "b", "t")
    h.sleep()
    p = prompts[-1]
    assert "(c1) [model] " + long_text[:120] + "...  (3 episode(s) already attached)" in p
    assert "one of its episodes: attempt 3 ran on the 4B" in p
    assert "(c2) Short." in p and "(c2) Short...." not in p


def test_a_memory_that_cannot_be_searched_means_no_cores_and_says_so(memory, make_hippo):
    from seren_hippocampus.memory_client import MemoryError as MemErr
    said: list[str] = []
    h = make_hippo()
    h._log = said.append

    def boom(q, n=5):
        raise MemErr("down")
    h._mem.search_long = boom
    assert h._candidates([{"content": "x"}]) == []
    assert any("candidate search failed" in m for m in said)
