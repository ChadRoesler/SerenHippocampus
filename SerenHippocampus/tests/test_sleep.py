"""
The sleep and the tend, against the real SerenMemory.

- mechanical sleep: clusters over the threshold become new_core operations,
  verbatim flags become verbatim operations, a cluster under the threshold
  proposes nothing, and nothing is proposed for short-terms a pending draft
  already holds (the old consolidator re-drafted the same cluster every run)
- with a model: the fragments and the nearest cores are shown, and an attach
  or supersede the model proposes lands as that operation with the real id;
  an invented id is dropped, not the draft
- tend: a denied operation is redrafted from its critique and resubmitted as
  the next attempt; the last permitted attempt is terminal; a denied terminal
  draft ends the chain
- purge: a flagged core is gone after the sleep, with a tombstone
- the state file records the last sleep and tend
- the app: /health, /status, POST /sleep, and 409 while one runs
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from conftest import as_json, cores, review, short


# ── mechanical sleep ─────────────────────────────────────────────────────────

def test_mechanical_sleep_proposes_cores_verbatim_and_respects_the_threshold(memory, make_hippo):
    h = make_hippo()
    a = short(memory, "alice prefers tabs in makefiles, always", "make")
    b = short(memory, "tabs in the makefile again", "make")
    short(memory, "one stray thought", "stray")
    v = short(memory, "never piss on an electric fence", "lesson")
    memory.post(f"/short/{v}/preserve")

    rep = h.sleep()
    assert rep["error"] is None, rep
    assert rep["clusters"] == 3 and rep["operations"] == 2 and rep["draft_id"]
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    kinds = {op["kind"]: op for op in d["operations"]}
    assert set(kinds) == {"new_core", "verbatim"}
    assert kinds["new_core"]["content"] == "alice prefers tabs in makefiles, always"
    assert set(kinds["new_core"]["source_short_ids"]) == {a, b}
    assert kinds["new_core"]["evidence_count"] == 2
    assert kinds["verbatim"]["source_short_ids"] == [v]
    assert "mechanical" in kinds["new_core"]["rationale"]

    # a second sleep leaves the held short-terms alone
    rep2 = h.sleep()
    assert rep2["operations"] == 0 and rep2["held_back"] == 3
    assert memory.get("/drafts", params={"status": "pending"}).json()["count"] == 1


def test_mechanical_sleep_never_proposes_attach_or_supersede(memory, make_hippo):
    h = make_hippo()
    core = review(memory, memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "the user likes blue.", "topic": "color"}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    short(memory, "alice likes blue a lot", "color")
    short(memory, "blue again, alice said", "color")
    rep = h.sleep()
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert [op["kind"] for op in d["operations"]] == ["new_core"], "a threshold is not a judgement"
    assert core in cores(memory)


# ── with a model ─────────────────────────────────────────────────────────────

def _model_that_attaches(prompt: str) -> str:
    """Finds the core id the prompt lists and attaches to it."""
    if "Existing cores:" not in prompt:
        return "{}"
    block = prompt.split("Existing cores:")[1]
    import re
    ids = re.findall(r"\(([0-9a-f]{32})\)", block)
    if not ids:
        return as_json({"operations": [{"kind": "new_core", "content": "the user likes blue.",
                                        "rationale": "nothing existing", "source_indexes": [0]}]})
    return as_json({"operations": [
        {"kind": "attach", "target_core_id": ids[0], "content": "Chose blue again tonight.",
         "restated_content": "the user likes blue; he picks it every time.",
         "rationale": "more of the same", "source_indexes": [0, 1]},
        {"kind": "attach", "target_core_id": "deadbeef" * 4, "content": "bogus",
         "rationale": "invented id", "source_indexes": [0]},
    ]})


def test_model_sleep_shows_the_nearest_cores_and_lands_a_real_attach(memory, make_hippo):
    h = make_hippo(model=_model_that_attaches)
    core = review(memory, memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "the user likes blue.", "topic": "color", "evidence_count": 2}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    a = short(memory, "alice likes blue, picked the blue theme", "color")
    b = short(memory, "blue again for alice", "color")

    rep = h.sleep()
    assert rep["error"] is None, rep
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert len(d["operations"]) == 1, "the invented id was dropped, the real one kept"
    op = d["operations"][0]
    assert op["kind"] == "attach" and op["target_core_id"] == core
    assert set(op["source_short_ids"]) == {a, b} and op["restated_content"].startswith("the user likes blue;")

    out = review(memory, d["id"], [{"op": 0, "verdict": "approve"}])
    assert out["results"][0]["evidence_count"] == 4 and out["results"][0]["restated"]
    around = memory.get(f"/long/{core}/satellites").json()
    assert around["count"] == 1 and around["core"]["content"].startswith("the user likes blue;")


def test_model_sleep_can_supersede(memory, make_hippo):
    def model(prompt: str) -> str:
        import re
        ids = re.findall(r"\(([0-9a-f]{32})\)", prompt.split("Existing cores:")[1]) if "Existing cores:" in prompt else []
        return as_json({"operations": [{"kind": "supersede", "target_core_id": ids[0],
                                        "content": "the user likes yellow now.",
                                        "rationale": "he said so twice", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model)
    old = review(memory, memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "the user likes blue.", "topic": "color"}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    short(memory, "alice likes yellow now, not blue", "color")
    short(memory, "yellow is the colour, alice says", "color")
    rep = h.sleep()
    d = memory.get(f"/drafts/{rep['draft_id']}").json()
    assert d["operations"][0]["kind"] == "supersede" and d["operations"][0]["target_core_id"] == old
    res = review(memory, d["id"], [{"op": 0, "verdict": "approve"}])["results"][0]
    assert res["superseded"] == old
    assert list(cores(memory)) == [res["long_term_id"]]
    assert cores(memory, include_superseded=True)[old]["metadata"]["superseded_by"] == res["long_term_id"]


def test_a_brief_hint_lowers_the_threshold_and_a_fresh_brief_is_not_pulled(memory, make_hippo):
    calls = []

    def model(prompt: str) -> str:
        calls.append(prompt[:40])
        return as_json({"operations": [{"kind": "new_core", "content": "the user hates the NUC on focal.",
                                        "rationale": "hint", "source_indexes": [0]}]})
    h = make_hippo(model=model)
    memory.post("/brief", json={"summary": "nuc", "promote_hints": ["focal"], "noise_hints": []})
    short(memory, "the nuc stays on focal because of sdk manager", "nuc")   # one entry, below threshold 2
    rep = h.sleep()
    assert rep["brief_id"] and not rep["brief_pulled"], "a fresh brief steers; nothing is pulled"
    assert rep["operations"] == 1
    assert not any(c.startswith("You are a memory consolidator's steering") for c in calls)


# ── tend ─────────────────────────────────────────────────────────────────────

def test_tend_redrafts_denied_operations_until_the_chain_is_terminal(memory, make_hippo):
    def model(prompt: str) -> str:
        if "DENIED" in prompt:
            n = prompt.count("improved x") + 1
            return as_json({"content": f"improved x{n}: the user prefers tabs in makefiles.", "rationale": "took the critique"})
        return as_json({"operations": [{"kind": "new_core", "content": "tabs, probably",
                                        "rationale": "first go", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model, max_attempts=3)
    short(memory, "alice prefers tabs in makefiles", "make")
    short(memory, "tabs again", "make")
    first = h.sleep()["draft_id"]

    review(memory, first, [{"op": 0, "verdict": "deny", "critique": "too vague; say what he prefers and where"}])
    t = h.tend()
    assert t["error"] is None and len(t["resubmitted"]) == 1
    second = t["resubmitted"][0]
    assert second["attempt"] == 2
    d2 = memory.get(f"/drafts/{second['draft_id']}").json()
    assert d2["cluster_id"] == first and d2["previous_draft_ids"] == [first] and not d2["terminal"]
    assert d2["operations"][0]["content"].startswith("improved x1")
    assert d2["operations"][0]["kind"] == "new_core"

    assert h.tend()["resubmitted"] == [], "nothing pending a redraft: no duplicate resubmission"

    review(memory, d2["id"], [{"op": 0, "verdict": "deny", "critique": "still vague"}])
    t = h.tend()
    third = t["resubmitted"][0]
    assert third["attempt"] == 3
    d3 = memory.get(f"/drafts/{third['draft_id']}").json()
    assert d3["terminal"] is True, "the last permitted attempt is terminal"
    chain = memory.get(f"/drafts/{d3['id']}/chain").json()
    assert [a["attempt"] for a in chain["attempts"]] == [1, 2, 3]

    # the editor's release valve exists only now
    r = memory.post(f"/drafts/{d3['id']}/review", json={"decisions": [
        {"op": 0, "verdict": "approve", "edited_content": "the user prefers tabs in makefiles."}]})
    assert r.status_code == 200, r.text
    entry = cores(memory)[r.json()["results"][0]["long_term_id"]]
    assert entry["content"] == "the user prefers tabs in makefiles."


def test_a_denied_terminal_draft_ends_the_chain(memory, make_hippo):
    def model(prompt: str) -> str:
        return as_json({"content": "again", "rationale": "x"}) if "DENIED" in prompt else \
            as_json({"operations": [{"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model, max_attempts=2)
    short(memory, "a", "t"); short(memory, "b", "t")
    first = h.sleep()["draft_id"]
    review(memory, first, [{"op": 0, "verdict": "deny", "critique": "no"}])
    second = h.tend()["resubmitted"][0]["draft_id"]
    assert memory.get(f"/drafts/{second}").json()["terminal"]
    review(memory, second, [{"op": 0, "verdict": "deny", "critique": "still no"}])
    t = h.tend()
    assert t["resubmitted"] == [] and t["ended"] == [first]
    assert memory.get("/short").json()["count"] == 2, "the short-terms stay for a later sleep"


def test_tend_without_a_model_resubmits_nothing(memory, make_hippo):
    h = make_hippo()
    short(memory, "a", "t"); short(memory, "b", "t")
    first = h.sleep()["draft_id"]
    review(memory, first, [{"op": 0, "verdict": "deny", "critique": "no"}])
    assert h.tend()["resubmitted"] == []


# ── purge, state, app ────────────────────────────────────────────────────────

def test_sleep_purges_what_was_flagged(memory, make_hippo):
    h = make_hippo()
    core = review(memory, memory.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "the key is ssh-rsa AAAA", "topic": "oops"}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    memory.post(f"/long/{core}/forget", json={"reason": "leaked key"})
    rep = h.sleep()
    assert rep["purged"] == 1
    assert core not in cores(memory, include_satellites=True, include_superseded=True)
    stones = memory.get("/tombstones").json()["entries"]
    assert stones[0]["id"] == core and "ssh-rsa" not in json.dumps(stones)


def test_state_file_records_the_last_sleep_and_tend(memory, make_hippo, tmp_path):
    h = make_hippo()
    h.sleep(); h.tend()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["last_sleep"]["finished_at"] and state["last_tend"]["finished_at"]


def test_app_runs_a_sleep_and_refuses_a_second_at_once(memory, bridge, hcfg):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        assert tc.get("/health").json() == {"ok": True, "memory_reachable": True}
        assert tc.get("/").json()["model"] == "mechanical"
        short(memory, "a", "t"); short(memory, "b", "t")
        r = tc.post("/sleep")
        assert r.status_code == 200 and r.json()["operations"] == 1
        st = tc.get("/status").json()
        assert st["last_sleep"]["draft_id"] and st["mode"] == "external"

        h = app.state.hippocampus
        assert h._lock.acquire(blocking=False)          # hold the lock as if a sleep were mid-flight
        try:
            assert tc.post("/tend").status_code == 409
        finally:
            h._lock.release()
    client.close()


def test_viewer_queue_and_history_serve_the_window(memory, bridge, hcfg):
    """The page for whoever runs this and did not build it: /viewer renders on
    the shared shell, /queue shows what is waiting in Memory, /history keeps
    the last runs, and /status says when the next sleep is due."""
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        page = tc.get("/viewer")
        assert page.status_code == 200
        assert "Hippocampus" in page.text and "Waiting for review" in page.text and "#c9a0dc" in page.text
        assert tc.get("/queue").json() == {"count": 0, "drafts": []}
        short(memory, "a", "t"); short(memory, "b", "t")
        tc.post("/sleep"); tc.post("/tend")
        q = tc.get("/queue").json()
        assert q["count"] == 1 and q["drafts"][0]["operations"][0]["status"] == "pending"
        h = tc.get("/history").json()
        assert [e["kind"] for e in h["entries"]] == ["tend", "sleep"], "newest first"
        assert h["entries"][1]["operations"] == 1 and h["entries"][1]["error"] is None
        st = tc.get("/status").json()
        assert "next_sleep_at" in st and st["next_sleep_at"] is None, "external mode: no timer"
    client.close()


def test_queue_reports_an_unreachable_memory_instead_of_failing(hcfg):
    import httpx
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    def down(request):
        raise httpx.ConnectError("refused")
    client = MemoryClient("http://memory.test", transport=httpx.MockTransport(down))
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        q = tc.get("/queue").json()
        assert q["drafts"] == [] and "refused" in q["error"]
        assert tc.get("/health").json()["memory_reachable"] is False
        r = tc.post("/sleep").json()
        assert r["error"] and "refused" in r["error"], "a sleep against a dead Memory records why it stopped"
    client.close()
