"""
The whole cycle, as drawn:

    sleep starts > the hippocampus asks for a brief > the brief arrives >
    draft > review > denied ones redraft, until the max > at the max the
    reviewer takes the best of the bunch, edits as needed and approves >
    write to long > age out

That morning a sleep started by hand skipped the brief and drafted unsteered;
its redraft put an episode's text where a core's rewording goes, and approving
it overwrote two cores; and on the last attempt the reviewer denied, so five
operations were dropped. Pinned here:

- a sleep by hand with no brief waiting asks for one and drafts nothing; the
  check sleeps on the brief when it arrives; without_brief drafts unsteered
- a brief already there is slept on at once; brief_request off sleeps as before
- a redraft never carries restated_content; text put there by mistake becomes
  the episode; it names the operation it replaces (redraft_of)
- the last attempt's ripple says to land things, and how
- the tick reports cores Memory put back (undo-restate), the same step that
  executes forget flags
"""
from __future__ import annotations

import pytest

from conftest import as_json, review, short

from seren_hippocampus.ripple import DEFAULT_MESSAGES, RESTATE


def _memory_has(feature: str) -> bool:
    try:
        from seren_memory.app import create_app  # noqa: F401
        import inspect
        import seren_memory.app as app
        return feature in inspect.getsource(app)
    except Exception:  # noqa: BLE001
        return False


needs_new_memory = pytest.mark.skipif(
    not _memory_has("restate_guard"),
    reason="the installed seren-memory predates the restate guard; runs once Memory ships it")

ONE = {"operations": [{"kind": "new_core", "content": "The model runs on port 7200.", "rationale": "a fact",
                       "source_indexes": [0]}]}


def _core(memory, content: str) -> str:
    did = memory.post("/drafts", json={"operations": [{"kind": "new_core", "content": content, "topic": "t"}]}).json()["id"]
    return review(memory, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


# ── sleep starts > asks for a brief ───────────────────────────────────────────
def test_a_sleep_by_hand_asks_for_a_brief_instead_of_drafting_blind(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json(ONE))
    short(memory, "the model runs on port 7200", "model")
    short(memory, "the model runs on port 7200, said again", "model")
    r = h.sleep(ask_for_brief=True)
    assert r["asked_for_brief"] is True and r["draft_id"] is None and r["operations"] == 0
    assert memory.get("/drafts").json()["count"] == 0, "nothing was drafted"
    assert h.state.get("last_sleep") is None, "no sleep is recorded, so bedtime does not move"
    ev = [e for e in h.state["events"] if e["event"] == "brief_requested"]
    assert len(ev) == 1 and ev[0]["intent_id"] == r["intent_id"]
    assert h.brief_wanted() is not None
    note = [n for n in memory.get("/near").json()["entries"] if n["id"] == r["intent_id"]]
    assert note and "started by hand" in note[0]["content"]

    # asked again before an answer: a nudge, not a second note
    h.sleep(ask_for_brief=True)
    assert len([e for e in h.state["events"] if e["event"] == "brief_requested"]) == 2
    assert h.state["brief_request"]["nudges"] == 1

    # the brief arrives; the check sleeps on it
    bid = memory.post("/brief", json={"summary": "the model moved", "promote_hints": ["model"]}).json()["id"]
    c = h.check()
    assert c["status"] == "sleeping" and c["sleep"]["brief_id"] == bid and c["sleep"]["draft_id"]
    assert c["sleep"]["brief_answered"] is True and h.brief_wanted() is None


def test_a_brief_already_waiting_is_slept_on_at_once(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json(ONE))
    short(memory, "the model runs on port 7200", "model")
    bid = memory.post("/brief", json={"summary": "s", "promote_hints": ["model"]}).json()["id"]
    r = h.sleep(ask_for_brief=True)
    assert r.get("asked_for_brief") is None and r["brief_id"] == bid and r["draft_id"]


def test_without_brief_and_where_nobody_can_be_asked_it_sleeps_as_before(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json(ONE))
    short(memory, "the model runs on port 7200", "model")
    short(memory, "the model runs on port 7200, said again", "model")
    r = h.sleep()                                           # the unsteered path, kept for the tests and the escape
    assert r.get("asked_for_brief") is None and r["brief_id"] is None and r["started_at"]


def test_where_nobody_can_be_asked_it_does_not_wait_for_ever(memory, make_hippo):
    h = make_hippo(model=lambda p: as_json(ONE), brief_request=False)
    short(memory, "the model runs on port 7200", "model")
    short(memory, "the model runs on port 7200, said again", "model")
    r = h.sleep(ask_for_brief=True)
    assert r.get("asked_for_brief") is None and r["started_at"]


async def test_the_tool_asks_by_default(memory, make_hippo):
    pytest.importorskip("mcp")
    from seren_hippocampus.mcp.tools import HippocampusToolImpl
    h = make_hippo(model=lambda p: as_json(ONE))
    short(memory, "the model runs on port 7200", "model")
    t = HippocampusToolImpl(h, h._mem, h._cfg)
    r = await t.sleep_now()
    assert r["ok"] is True and r["asked_for_brief"] is True and "submit_brief" in r["message"]
    r = await t.sleep_now(without_brief=True)
    assert r.get("asked_for_brief") is None and r["report"]["started_at"]


# ── a redraft never rewords a core ────────────────────────────────────────────
def _denied(memory, h, op: dict) -> str:
    a = short(memory, "attempt 3 of the first chain ran on the 4B", "t")
    did = memory.post("/drafts", json={"operations": [{**op, "source_short_ids": [a], "topic": "t"}],
                                       "attempt": 1, "extra": h._stamp()}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "make it an attach on the model core"}])
    return did


def test_a_redraft_drops_restated_content_and_names_what_it_replaces(memory, make_hippo):
    core = _core(memory, "The model core: Qwen3-4B on port 7200, 44 tokens a second.")
    episode = "On 26 Sept 2026 attempt 3 of the first chain ran on the 4B."
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": core, "content": episode,
                                             "restated_content": episode, "rationale": "the episode"}))
    h._candidates = lambda entries: [{"id": core, "content": "The model core...", "topic": "t"}]  # type: ignore
    _denied(memory, h, {"kind": "new_core", "content": "x"})
    t = h.tend()
    op = memory.get(f"/drafts/{t['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert (op["kind"], op["target_core_id"], op["content"]) == ("attach", core, episode)
    assert not op.get("restated_content"), "a redraft cannot reword a core it saw 300 characters of"
    if "redraft_of" in op:                                  # an older Memory drops the field
        assert op["redraft_of"] == 0


def test_text_put_in_the_wrong_field_becomes_the_episode(memory, make_hippo):
    """1 Oct 2026: 'content is empty and the lifecycle episode sits in
    restated_content, so approving would overwrite the core.'"""
    core = _core(memory, "The model core: Qwen3-4B on port 7200, 44 tokens a second.")
    episode = "On 25 Sept 2026 the model lifecycle was built."
    h = make_hippo(model=lambda p: as_json({"kind": "attach", "target_core_id": core, "content": "",
                                             "restated_content": episode}))
    h._candidates = lambda entries: [{"id": core, "content": "The model core...", "topic": "t"}]  # type: ignore
    _denied(memory, h, {"kind": "new_core", "content": "x"})
    op = memory.get(f"/drafts/{h.tend()['resubmitted'][0]['draft_id']}").json()["operations"][0]
    assert op["content"] == episode and not op.get("restated_content")


def test_the_redraft_prompt_no_longer_offers_a_rewording(memory, make_hippo):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        return as_json({"content": "reworded"})
    h = make_hippo(model=model)
    _denied(memory, h, {"kind": "new_core", "content": "x"})
    h.tend()
    assert "restated_content" not in prompts[-1]


@needs_new_memory
def test_the_reviewer_sees_every_version_and_lands_the_last_one(memory, make_hippo):
    """The max is reached: the best of the bunch, edited, approved - and the
    chain closes with it in long-term."""
    core = _core(memory, "The model core: Qwen3-4B on port 7200, 44 tokens a second.")
    h = make_hippo(model=lambda p: as_json({"kind": "supersede", "target_core_id": core,
                                             "content": "On 26 Sept 2026 attempt 3 ran on the 4B."}),
                   max_attempts=2)
    h._candidates = lambda entries: [{"id": core, "content": "The model core...", "topic": "t"}]  # type: ignore
    first = _denied(memory, h, {"kind": "new_core", "content": "attempt three, vaguely"})
    t = h.tend()
    last = t["resubmitted"][0]["draft_id"]
    ev = [e for e in h.state["events"] if e["event"] == "tend_resubmitted"][-1]
    assert ev["terminal"] is True

    view = memory.get(f"/drafts/{last}", params={"review": True}).json()
    op = view["operations"][0]
    assert "last attempt" in view["last_attempt"]
    assert op["target_core"]["content"].startswith("The model core")
    assert op["earlier_attempts"][0]["content"] == "attempt three, vaguely"
    assert op["earlier_attempts"][0]["critique"] == "make it an attach on the model core"

    # a supersede would demote the model core; the reviewer makes it the attach it should be
    out = review(memory, last, [{"op": 0, "verdict": "approve", "edited_kind": "attach"}])
    assert out["approved"] == 1 and out["denied"] == 0
    sats = memory.get(f"/long/{core}/satellites").json()
    assert sats["count"] == 1 and "Qwen3-4B on port 7200" in sats["core"]["content"]
    assert not sats["core"]["metadata"].get("superseded_by")

    t = h.tend()
    assert first in t["closed"] and t["ended"] == [], "it landed: the chain closes, it does not end denied"
    assert not h._open_chain()


# ── the ripple for the last attempt ───────────────────────────────────────────
def test_the_last_attempt_is_told_to_land_things(make_hippo):
    h = make_hippo()
    msg = h.ripple.message({"event": "tend_resubmitted", "draft_id": "d" * 32, "terminal": True})
    assert "LAST attempt" in msg and "d" * 32 in msg
    for word in ("edited_content", "edited_kind", "edited_target_core_id", "earlier_attempts", '"restate": false'):
        assert word in msg, word
    assert msg.startswith("You were woken by the hippocampus")

    earlier = h.ripple.message({"event": "tend_resubmitted", "draft_id": "d" * 32, "terminal": False})
    assert "LAST attempt" not in earlier and "edited_kind" not in earlier and RESTATE.strip() in earlier
    assert RESTATE.strip() in h.ripple.message({"event": "draft_submitted", "draft_id": "d" * 32})


def test_an_operators_own_wording_still_wins(make_hippo):
    h = make_hippo()
    h._cfg.ripple.messages = {"tend_resubmitted": "mine {draft_id}"}
    assert h.ripple.message({"event": "tend_resubmitted", "draft_id": "x", "terminal": True}) == "mine x"
    h._cfg.ripple.messages = {"tend_resubmitted": "mine", "tend_resubmitted_terminal": "last one {draft_id}"}
    assert h.ripple.message({"event": "tend_resubmitted", "draft_id": "x", "terminal": True}) == "last one x"
    assert h.ripple.message({"event": "tend_resubmitted", "draft_id": "x"}) == "mine"
    assert "tend_resubmitted_terminal" in DEFAULT_MESSAGES


# ── the gated way back ────────────────────────────────────────────────────────
@needs_new_memory
def test_the_tick_carries_out_a_flagged_restore_and_says_so(memory, make_hippo):
    core_text = ("The user, on why they build this with the assistant: it helps break things down, and pushes back; "
                 "'people pack bond, and you are part of it now.'")
    core = _core(memory, core_text)
    ep = "They said the assistant would like pluots, the juiciest thing they have ever eaten."
    did = memory.post("/drafts", json={"operations": [
        {"kind": "attach", "content": ep, "topic": "t", "target_core_id": core, "restated_content": ep}]}).json()["id"]
    review(memory, did, [{"op": 0, "verdict": "approve", "restate": True}])      # the mistake, made on purpose
    assert memory.get(f"/long/{core}/satellites").json()["core"]["content"] == ep

    memory.app.state.store.flag_undo_restate(core, "approved without comparing")   # the model's tool does this
    assert memory.get(f"/long/{core}/satellites").json()["core"]["content"] == ep, "a flag changes nothing"

    said: list[str] = []
    h = make_hippo()
    h._log = said.append
    h.check()
    got = memory.get(f"/long/{core}/satellites").json()["core"]
    assert got["content"] == core_text and got["metadata"]["restate_undone_reason"] == "approved without comparing"
    ev = [e for e in h.state["events"] if e["event"] == "restored"]
    assert ev and ev[-1]["ids"] == [core] and any("earlier wording" in m for m in said)
    h.check()
    assert len([e for e in h.state["events"] if e["event"] == "restored"]) == 1, "done once"
