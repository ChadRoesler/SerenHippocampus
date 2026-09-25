"""
The brief is not only a threshold lever. What the main model said mattered
goes IN FRONT of the drafting worker, so a running bit is drafted as a core
and a one-off is left alone on the brief's say-so, not on a count.

Chad, 24 Sept 2026: "a one off mention of a dream about a fish doesn't make
it to long, because it's a one off, but a consistent joke about how Fred Durst
is the greatest philosopher and warrior poet of our time" does. The brief is
what turns a one-off joke into an inside joke.
"""
from __future__ import annotations

from conftest import as_json, short


def _capture(prompts: list[str]):
    def model(prompt: str) -> str:
        prompts.append(prompt)
        return as_json({"operations": [{"kind": "new_core", "content": "Fred Durst is the warrior poet of our time.",
                                        "rationale": "the bit", "source_indexes": [0]}]})
    return model


def test_the_briefs_summary_and_the_matched_hint_reach_the_draft_prompt(memory, make_hippo):
    prompts: list[str] = []
    h = make_hippo(model=_capture(prompts))
    memory.post("/brief", json={
        "summary": "Chad kept coming back to Fred Durst as the greatest philosopher and warrior poet of our time. "
                   "The fish dream was a one-off.",
        "promote_hints": ["fred durst"], "noise_hints": ["fish dream"]})
    short(memory, "Chad: Fred Durst is the greatest philosopher of our time, it's just one of those days", "durst")
    rep = h.sleep()
    assert rep["brief_id"] and rep["operations"] == 1
    drafts = [p for p in prompts if "drafting worker" in p]
    assert len(drafts) == 1
    p = drafts[0]
    assert "Steer:" in p and "written by the main model, who was there" in p
    assert "warrior poet" in p, "the summary itself is in the prompt, not only the hint"
    assert "names THIS topic as worth keeping (fred durst)" in p
    assert "as noise" not in p, "the fish-dream hint did not match this cluster and is not mentioned"


def test_a_noise_hint_keeps_the_one_off_out_and_says_so_when_pinned(memory, make_hippo):
    prompts: list[str] = []
    h = make_hippo(model=_capture(prompts), promote_min_evidence=1)
    memory.post("/brief", json={"summary": "quiet day", "promote_hints": [], "noise_hints": ["fish dream"]})
    short(memory, "I had a fish dream last night", "dreams")
    rep = h.sleep()
    assert rep["operations"] == 0 and not prompts, "a one-off named as noise is not even drafted"
    # pinned by the person: the worker still runs, and is told the brief's call so it can weigh it.
    # A brief steers ONE sleep (the next one after it was written), so the day's brief is written again.
    memory.post("/brief", json={"summary": "still quiet", "promote_hints": [], "noise_hints": ["fish dream"]})
    short(memory, "the fish dream again, actually", "dreams", pinned=True)
    h.sleep()
    p = next(p for p in prompts if "drafting worker" in p)
    assert "names THIS topic as noise (fish dream)" in p


def test_a_pulled_brief_is_marked_as_the_small_models_guess(memory, make_hippo):
    """brief_pull is the headless path: past bedtime, after the misses, the
    small model pulls a brief and the steer says so."""
    import time
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        if "steering" in prompt or "JSON brief" in prompt:
            return as_json({"summary": "the box talk mattered", "promote_hints": ["nuc"],
                            "noise_hints": [], "completed_intents": []})
        return as_json({"operations": []})
    h = make_hippo(model=model, brief_pull=True, interval_seconds=3600, brief_check_misses=1)
    h.state["last_sleep"] = {"finished_at": time.time() - 4000, "error": None}
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    rep = h.check()
    assert rep["status"] == "sleeping_on_pulled_brief" and rep["sleep"]["brief_pulled"] is True
    p = next(p for p in prompts if "drafting worker" in p)
    assert "pulled from the fragments by a small model" in p and "the box talk mattered" in p


def test_no_brief_no_steer_block(memory, make_hippo):
    prompts: list[str] = []
    h = make_hippo(model=_capture(prompts))
    h._pull_brief = lambda: None  # type: ignore[method-assign]
    short(memory, "a", "t"); short(memory, "b", "t")
    h.sleep()
    p = next(p for p in prompts if "drafting worker" in p)
    assert "Steer:" not in p
