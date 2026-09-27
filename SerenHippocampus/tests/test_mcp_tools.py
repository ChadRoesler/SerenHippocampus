"""
The MCP tools: the main model's view of the sleep, and its hands on it.

Called DIRECTLY on HippocampusToolImpl - no FastMCP, no HTTP - against the
real in-process Memory the rest of these tests use, the way SerenMemory tests
its own tools. Pinned here:

- sleep_status says where things stand, in fields and in a sentence, and
  still answers when Memory does not
- sleep_now / tend_now / check_now run what POST /sleep, /tend, /check run,
  and a sleep or tend already running is busy: true, never an exception
- a dead Memory is a message, and is NOT recorded as a failed sleep (that
  would move bedtime and fire sleep_failed at the webhook for nothing)
- sleep_history, audit_sleeps (trimmed: numbers and verdicts, no wording),
  list_replays and replay_draft (trimmed: no prompts, nothing submitted)

Gated on the `mcp` SDK: without the [mcp] extra the file is collected and
skipped, so CI without it passes.
"""
from __future__ import annotations

import inspect
import time

import httpx
import pytest

try:
    import mcp  # noqa: F401
    from seren_hippocampus.mcp.tools import TOOL_NAMES, HippocampusToolImpl, register_tools
    _mcp_available = True
except ImportError:
    _mcp_available = False
    HippocampusToolImpl = None  # type: ignore

pytestmark = pytest.mark.skipif(not _mcp_available, reason="mcp extras not installed")

from conftest import as_json, review, short
from seren_hippocampus.memory_client import MemoryClient
from seren_hippocampus.sleep import Hippocampus


def _tools(h: Hippocampus, next_at=None) -> "HippocampusToolImpl":
    return HippocampusToolImpl(h, h._mem, h._cfg, next_at=next_at)


def _brief(memory, summary="what mattered", promote=None) -> str:
    return memory.post("/brief", json={"summary": summary, "promote_hints": promote or [],
                                       "noise_hints": []}).json()["id"]


def _model(prompt: str) -> str:
    if "DENIED" in prompt:
        return as_json({"content": "The NUC stays on focal.", "rationale": "from the critique"})
    return as_json({"operations": [{"kind": "new_core", "content": "The NUC moved to jammy.",
                                    "rationale": "said twice", "source_indexes": [0, 1]}]})


@pytest.fixture
def dead_memory(hcfg):
    """A Memory that never answers: every request is a refused connection."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)
    client = MemoryClient("http://memory.down", transport=httpx.MockTransport(refuse))
    yield Hippocampus(hcfg, client, log=lambda m: None)
    client.close()


# ── status ──────────────────────────────────────────────────────────────────
async def test_status_on_a_fresh_install_says_so(make_hippo):
    s = await _tools(make_hippo()).sleep_status()
    assert s["memory"]["reachable"] is True and s["busy"] is False
    assert s["last_sleep"] is None and "No sleep on record" in s["say"]
    assert s["brief_waiting"] is False and s["chain_open"] is False and s["pending_review"] == 0
    assert s["model"]["mode"] == "mechanical" and "mechanical" in s["say"]
    assert s["bedtime"] and s["past_bedtime"] is True, "no sleep on record: due now"


async def test_status_follows_a_brief_through_a_sleep_into_review(memory, make_hippo):
    h = make_hippo()
    t = _tools(h, next_at={"tick": None})
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory)
    s = await t.sleep_status()
    assert s["brief_waiting"] is True and s["brief_id"] == bid and s["chain_open"] is False
    assert "A brief is waiting" in s["say"]

    c = await t.check_now()
    did = c["report"]["sleep"]["draft_id"]
    assert c["ok"] and c["status"] == "sleeping" and did in c["message"] and "review_draft" in c["message"]

    s = await t.sleep_status()
    assert s["last_sleep"]["ok"] is True and s["last_sleep"]["draft_id"] == did
    assert s["pending_review"] == 1 and s["chain_open"] is True
    assert s["last_check"]["status"] == "sleeping" and s["last_sleep"]["when"].endswith(("ago)", "just now)"))
    assert "wait for your review" in s["say"]


async def test_status_answers_when_memory_does_not(dead_memory):
    s = await _tools(dead_memory).sleep_status()
    assert s["memory"]["reachable"] is False and s["memory"]["error"]
    assert s["brief_waiting"] is None and s["chain_open"] is None
    assert "not answering" in s["say"]


# ── acting ──────────────────────────────────────────────────────────────────
async def test_sleep_now_runs_a_sleep_on_the_open_brief(memory, make_hippo):
    h = make_hippo()
    short(memory, "a", "t"); short(memory, "b", "t")
    bid = _brief(memory)
    r = await _tools(h).sleep_now()
    assert r["ok"] is True and r["report"]["brief_id"] == bid and r["report"]["operations"] == 1
    assert r["report"]["draft_id"] in r["message"]
    assert memory.get("/drafts", params={"status": "pending"}).json()["count"] == 1


async def test_sleep_now_refuses_while_a_cycle_is_under_way(memory, make_hippo):
    """One cycle at a time: a second sleep_now with a draft under review
    starts nothing and says why - no second draft, the brief not consumed."""
    h = make_hippo()
    t = _tools(h)
    short(memory, "a", "t"); short(memory, "b", "t")
    _brief(memory)
    assert (await t.sleep_now())["ok"] is True
    short(memory, "c", "u"); short(memory, "d", "u")
    _brief(memory)
    r = await t.sleep_now()
    assert r["ok"] is False and r["refused"] is True and r["report"]["status"] == "chain_open"
    assert r["message"].startswith("Nothing started") and "under way" in r["message"]
    assert memory.get("/drafts", params={"status": "pending"}).json()["count"] == 1
    assert (await t.sleep_status())["brief_waiting"], "the second brief waits its turn"


async def test_a_quiet_sleep_says_it_was_quiet(make_hippo):
    r = await _tools(make_hippo()).sleep_now()
    assert r["ok"] is True and r["report"]["quiet"] is True and "quiet" in r["message"]


async def test_busy_is_a_message_for_every_run_tool(make_hippo):
    h = make_hippo()
    t = _tools(h)
    assert h._lock.acquire(blocking=False)             # as if a sleep were mid-flight
    try:
        for run in (t.sleep_now, t.tend_now, t.check_now):
            r = await run()
            assert r["ok"] is False and r["busy"] is True and "already running" in r["message"]
        assert (await t.sleep_status())["busy"] is True
    finally:
        h._lock.release()


async def test_a_dead_memory_is_a_message_and_not_a_failed_sleep(dead_memory):
    t = _tools(dead_memory)
    for run in (t.sleep_now, t.tend_now, t.check_now, t.audit_sleeps):
        r = await run()
        assert r["ok"] is False and r["memory_reachable"] is False and "not answering" in r["message"]
    assert dead_memory.state.get("last_sleep") is None, "nothing ran, so nothing moves bedtime"
    assert not [e for e in dead_memory.state.get("events") or [] if e["event"] == "sleep_failed"]


async def test_tend_now_redrafts_a_denial_and_says_so(memory, make_hippo):
    h = make_hippo(model=_model)
    t = _tools(h)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    _brief(memory)
    did = (await t.sleep_now())["report"]["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: it stayed on focal"}])
    r = await t.tend_now()
    assert r["ok"] is True and len(r["report"]["resubmitted"]) == 1 and "resubmitted 1" in r["message"]
    assert (await t.tend_now())["message"].startswith("Nothing to tend"), "the redraft waits for review"


async def test_check_now_respects_the_gate(memory, make_hippo):
    h = make_hippo(interval_seconds=72000)
    h.state["last_sleep"] = {"finished_at": time.time() - 60, "error": None}
    short(memory, "a", "t"); short(memory, "b", "t")
    r = await _tools(h).check_now()
    assert r["ok"] is True and r["status"] == "not_bedtime" and "not bedtime" in r["message"]
    assert memory.get("/drafts").json()["count"] == 0, "no brief, no sleep"


# ── reading what happened ───────────────────────────────────────────────────
async def test_history_is_newest_first_and_readable(memory, make_hippo):
    h = make_hippo()
    t = _tools(h)
    short(memory, "a", "t"); short(memory, "b", "t")
    await t.sleep_now()
    await t.tend_now()
    got = await t.sleep_history(limit=5)
    assert got["count"] == 2 and [e["kind"] for e in got["entries"]] == ["tend", "sleep"]
    assert got["entries"][1]["draft_id"] and got["entries"][1]["when"]
    assert len((await t.sleep_history(limit=1))["entries"]) == 1


async def test_audit_is_the_numbers_and_the_verdicts_without_the_wording(memory, make_hippo):
    h = make_hippo(model=_model)
    t = _tools(h)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    _brief(memory, "nuc talk")
    did = (await t.sleep_now())["report"]["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: it stayed on focal"}])
    again = (await t.tend_now())["report"]["resubmitted"][0]["draft_id"]
    review(memory, again, [{"op": 0, "verdict": "approve"}])

    a = await t.audit_sleeps(limit=3)
    assert a["ok"] is True and a["chain_count"] == 1 and a["models"]
    assert a["models"][0]["drafts"] == 2 and a["models"][0]["first_pass_rate"] == 0.0
    chain = a["chains"][0]
    assert chain["brief"]["summary"] == "nuc talk" and chain["landed"] == 1
    first, second = chain["attempts"]
    assert first["denied"] == 1 and first["critiques"] == ["DENIED: it stayed on focal"]
    assert second["approved"] == 1 and second["attempt"] == 2
    assert "jammy" not in str(chain["attempts"]), "operation wording stays on Memory's audit_drafts"


async def test_replays_list_and_replay_without_submitting(memory, make_hippo):
    h = make_hippo(model=_model)
    t = _tools(h)
    short(memory, "the nuc stays on focal", "nuc"); short(memory, "the nuc hates jammy", "nuc")
    did = (await t.sleep_now())["report"]["draft_id"]
    review(memory, did, [{"op": 0, "verdict": "approve"}])

    rows = (await t.list_replays())["entries"]
    assert rows[0]["draft_id"] == did and rows[0]["calls"] == 1 and rows[0]["when"]

    before = memory.get("/drafts", params={"limit": 100}).json()["count"]
    r = await t.replay_draft(did)                     # empty url: the configured (faked) model
    assert memory.get("/drafts", params={"limit": 100}).json()["count"] == before, "a replay never submits"
    assert r["ok"] is True and r["landed"] == ["The NUC moved to jammy."]
    assert r["candidate"]["checks"]["valid"] == 1 and r["candidate"]["failures"] == []
    assert r["original"]["verdicts"][0]["status"] == "approved"
    assert "Fragments:" not in str(r), "prompts are left out to keep it small"

    miss = await t.replay_draft("no-such-draft")
    assert miss["ok"] is False and "no replay packet" in miss["message"]


async def test_replay_in_mechanical_mode_asks_for_a_url(make_hippo):
    r = await _tools(make_hippo()).replay_draft("anything")
    assert r["ok"] is False and "url=" in r["message"]


# ── registration ────────────────────────────────────────────────────────────
async def test_register_tools_serves_the_roster_as_coroutines(make_hippo):
    from mcp.server.fastmcp import FastMCP
    h = make_hippo()
    server = FastMCP("t")
    impl = register_tools(server, h, h._mem, h._cfg)
    names = sorted(tool.name for tool in await server.list_tools())
    assert names == sorted(TOOL_NAMES)
    # A sync tool runs ON the event loop; a sleep there would freeze the service.
    assert all(inspect.iscoroutinefunction(getattr(impl, n)) for n in TOOL_NAMES)
