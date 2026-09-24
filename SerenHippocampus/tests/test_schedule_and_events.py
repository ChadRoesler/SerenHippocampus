"""
When the sleep fires, what a long gap does to it, when it stays quiet, and
how it says what happened.

- a fresh install (no sleep on record) is due after the warm-up, not a whole
  interval later; a sleep on record is due one interval after it; a process
  that boots overdue is also due after the warm-up
- sleep.at pins the sleep to a local wall-clock time: the next occurrence
  after the last sleep
- a month away: the first sleep back is a CATCH-UP. It drafts and purges but
  does not age out or sweep, so nothing that never had its chance is gone
  before someone has looked. A sleep inside the grace window is a normal one
- quiet: with nothing free to draft from, no brief is pulled and no model is
  called; the report says so
- events: a submitted docket, a purge, a failed sleep and an ended chain are
  recorded on the service (GET /events) and, with a webhook, POSTed with the
  bearer; a webhook that fails never fails the sleep
"""
from __future__ import annotations

import datetime as dt
import json
import time

import httpx
import pytest

from conftest import as_json, review, short

from seren_hippocampus.config import NotifyConfig


def _tidy_spy(h):
    """Record the flags every tidy call went out with."""
    calls: list[dict] = []
    real = h._mem.tidy

    def spy(**kw):
        calls.append(dict(kw))
        return real(**kw)
    h._mem.tidy = spy  # type: ignore[method-assign]
    return calls


def _slept(h, ago_seconds: float) -> None:
    h.state["last_sleep"] = {"finished_at": time.time() - ago_seconds, "error": None}


# ── when the sleep is due ────────────────────────────────────────────────────

def test_a_fresh_install_sleeps_after_the_warm_up_and_a_recorded_one_an_interval_later(make_hippo):
    h = make_hippo(interval_seconds=3600, warmup_seconds=120)
    now = time.time()
    assert h.next_sleep_due(now) <= now, "nothing on record: due now"
    assert h.seconds_until_sleep(now) == 120, "...which the loop turns into one warm-up"
    _slept(h, 600)
    assert 2990 < h.seconds_until_sleep() < 3001, "one interval after the last sleep"


def test_a_process_that_boots_overdue_sleeps_after_the_warm_up_not_an_interval(make_hippo):
    h = make_hippo(interval_seconds=3600, warmup_seconds=300)
    _slept(h, 30 * 24 * 3600)                              # a month with the computer off
    assert h.next_sleep_due() < time.time()
    assert h.seconds_until_sleep() == 300


def test_sleep_at_pins_the_next_sleep_to_the_wall_clock(make_hippo):
    h = make_hippo(at="03:30", interval_seconds=3600)
    # last sleep finished yesterday at 03:31 local -> due today 03:30
    last = (dt.datetime.now() - dt.timedelta(days=1)).replace(hour=3, minute=31, second=0, microsecond=0)
    h.state["last_sleep"] = {"finished_at": last.timestamp()}
    due = dt.datetime.fromtimestamp(h.next_sleep_due())
    assert (due.hour, due.minute) == (3, 30) and due.date() == dt.date.today()
    # a manual sleep at 14:00 today -> tomorrow 03:30, not today's (already passed)
    h.state["last_sleep"] = {"finished_at": dt.datetime.now().replace(hour=14, minute=0, second=0).timestamp()}
    due = dt.datetime.fromtimestamp(h.next_sleep_due(dt.datetime.now().replace(hour=14, minute=5).timestamp()))
    assert due.date() == dt.date.today() + dt.timedelta(days=1) and (due.hour, due.minute) == (3, 30)
    # a bad value falls back to the interval instead of crashing the loop
    h = make_hippo(at="half past three", interval_seconds=3600)
    _slept(h, 0)
    assert 3599 < h.seconds_until_sleep() <= 3600


# ── the month away ───────────────────────────────────────────────────────────

def test_a_sleep_after_a_long_gap_is_a_catch_up_that_ages_nothing_out(memory, make_hippo):
    h = make_hippo(interval_seconds=3600, gap_grace_intervals=3)
    calls = _tidy_spy(h)
    _slept(h, 30 * 24 * 3600)
    short(memory, "the forest has no wifi", "away"); short(memory, "the forest still has no wifi", "away")
    assert h.is_catch_up()
    rep = h.sleep()
    assert rep["catch_up"] is True and rep["error"] is None
    assert rep["operations"] == 1, "it still drafts"
    final = calls[-1]
    assert final["age_out"] is False and final["sweep"] is False and final["near"] is True, \
        "a catch-up never ages out or sweeps: nothing is trashed before someone has looked"
    assert calls[0] == {"age_out": False, "near": False, "sweep": False, "purge": True}, "purge still runs first"
    assert [e["event"] for e in h.state["events"]][:1] == ["catch_up"]


def test_a_sleep_inside_the_grace_window_is_a_normal_one(memory, make_hippo):
    h = make_hippo(interval_seconds=3600, gap_grace_intervals=3)
    calls = _tidy_spy(h)
    _slept(h, 2 * 3600)                                    # missed one interval: normal
    assert not h.is_catch_up()
    rep = h.sleep()
    assert rep["catch_up"] is False
    assert calls[-1]["age_out"] is True and calls[-1]["sweep"] is True


def test_the_very_first_sleep_on_a_store_is_a_catch_up(memory, make_hippo):
    h = make_hippo()
    calls = _tidy_spy(h)
    rep = h.sleep()
    assert rep["catch_up"] is True and calls[-1]["age_out"] is False


# ── quiet ────────────────────────────────────────────────────────────────────

def test_a_sleep_with_nothing_to_draft_from_calls_no_model(memory, make_hippo):
    calls: list[str] = []

    def model(prompt: str) -> str:
        calls.append(prompt)
        return as_json({"operations": []})
    h = make_hippo(model=model)
    _slept(h, 60)
    rep = h.sleep()
    assert rep["quiet"] is True and rep["brief_pulled"] is False and rep["operations"] == 0
    assert calls == [], "no brief pulled, no draft asked for"
    assert rep["error"] is None
    assert h.state["history"][-1]["quiet"] is True
    # something arrives: the next sleep is not quiet
    short(memory, "back to it", "t"); short(memory, "back to it again", "t")
    rep = h.sleep()
    assert rep["quiet"] is False and calls, "a fragment to draft from wakes the model"


def test_short_terms_held_by_a_pending_docket_do_not_break_the_quiet(memory, make_hippo):
    h = make_hippo()
    short(memory, "a", "t"); short(memory, "b", "t")
    first = h.sleep()
    assert first["operations"] == 1 and first["quiet"] is False
    again = h.sleep()                                       # the same shorts, now held under review
    assert again["quiet"] is True and again["operations"] == 0


# ── events and the webhook ───────────────────────────────────────────────────

def test_events_are_kept_and_posted_to_the_webhook_with_the_bearer(memory, make_hippo):
    posted: list[httpx.Request] = []

    def hook(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(204)
    h = make_hippo()
    h._cfg.notify = NotifyConfig(webhook_url="http://lodestar.test/hooks/hippocampus", bearer_token="s3cret",
                                 events=["docket_submitted", "purged", "sleep_failed", "chain_ended"])
    h._notify_transport = httpx.MockTransport(hook)
    short(memory, "a", "t"); short(memory, "b", "t")
    rep = h.sleep()
    kinds = [e["event"] for e in h.state["events"]]
    assert "docket_submitted" in kinds and "sleep_done" in kinds
    sent = [json.loads(r.content) for r in posted]
    assert [s["event"] for s in sent] == ["catch_up", "docket_submitted"] or [s["event"] for s in sent] == ["docket_submitted"], sent
    sub = next(s for s in sent if s["event"] == "docket_submitted")
    assert sub["docket_id"] == rep["docket_id"] and sub["operations"] == 1 and sub["service"] == "seren-hippocampus"
    assert posted[-1].headers["authorization"] == "Bearer s3cret"
    assert all(e.get("delivered") is True for e in h.state["events"] if e["event"] == "docket_submitted")
    assert "sleep_done" not in [s["event"] for s in sent], "only the configured events go out"


def test_a_failing_webhook_never_fails_the_sleep(memory, make_hippo):
    def hook(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nobody home")
    h = make_hippo()
    h._cfg.notify = NotifyConfig(webhook_url="http://lodestar.test/x")
    h._notify_transport = httpx.MockTransport(hook)
    short(memory, "a", "t"); short(memory, "b", "t")
    rep = h.sleep()
    assert rep["error"] is None and rep["operations"] == 1
    ev = next(e for e in h.state["events"] if e["event"] == "docket_submitted")
    assert ev["delivered"] is False and "nobody home" in ev["delivery_error"]


def test_a_purge_a_failure_and_an_ended_chain_are_events(memory, make_hippo):
    def model(prompt: str) -> str:
        return as_json({"content": "again", "rationale": "x"}) if "DENIED" in prompt else             as_json({"operations": [{"kind": "new_core", "content": "x", "rationale": "x", "source_indexes": [0, 1]}]})
    h = make_hippo(model=model, max_attempts=2)
    core = review(memory, memory.post("/dockets", json={"operations": [
        {"kind": "new_core", "content": "the key is ssh-rsa AAAA", "topic": "oops"}]}).json()["id"],
        [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    memory.post(f"/long/{core}/forget", json={"reason": "leaked key"})
    short(memory, "a", "t"); short(memory, "b", "t")
    rep = h.sleep()
    purged = next(e for e in h.state["events"] if e["event"] == "purged")
    assert purged["count"] == 1 and purged["ids"] == [core]
    assert "ssh-rsa" not in json.dumps(h.state["events"]), "an event carries ids, never content"
    # deny both permitted attempts: the chain is spent; tend says so once
    review(memory, rep["docket_id"], [{"op": 0, "verdict": "deny", "critique": "no"}])
    second = h.tend()["resubmitted"][0]["docket_id"]
    resub = next(e for e in h.state["events"] if e["event"] == "tend_resubmitted")
    assert resub["docket_id"] == second and resub["attempt"] == 2 and resub["terminal"] is True
    review(memory, second, [{"op": 0, "verdict": "deny", "critique": "still no"}])
    h.tend(); h.tend()
    kinds = [e["event"] for e in h.state["events"]]
    assert kinds.count("chain_ended") == 1, "said once, not every five minutes"

    # a dead Memory is a sleep_failed event
    real = h._mem.tidy

    def dead(**kw):
        raise httpx.ConnectError("refused")
    h._mem.tidy = dead  # type: ignore[method-assign]
    rep = h.sleep()
    h._mem.tidy = real  # type: ignore[method-assign]
    assert rep["error"] and h.state["events"][-1]["event"] == "sleep_failed"


def test_the_app_serves_events_and_status_says_what_the_next_sleep_will_be(memory, bridge, hcfg):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient

    hcfg.sleep.at = "03:30"
    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        st = tc.get("/status").json()
        assert st["catch_up_next"] is True and st["sleep_at"] == "03:30" and st["webhook"] is False
        assert tc.get("/events").json() == {"count": 0, "webhook": False, "entries": []}
        short(memory, "a", "t"); short(memory, "b", "t")
        tc.post("/sleep")
        ev = tc.get("/events").json()
        assert ev["count"] >= 2 and ev["entries"][0]["event"] == "sleep_done", "newest first"
        assert any(e["event"] == "docket_submitted" for e in ev["entries"])
        assert tc.get("/status").json()["catch_up_next"] is False
        page = tc.get("/viewer").text
        assert "events-list" in page and "renderEvents" in page
    client.close()
