"""
seren_hippocampus.mcp.tools
═══════════════════════════

The tools the MCP server exposes: the main model's view of the sleep, and its
hands on it. Each is a thin wrapper over the in-process Hippocampus and
MemoryClient the HTTP routes use (app.state.hippocampus / app.state.memory).

STRUCTURE

`HippocampusToolImpl` holds every tool as a method. `register_tools` wires
each method onto a FastMCP instance via `@mcp.tool()`. The split exists for
testability - `HippocampusToolImpl(...).sleep_status()` is directly callable
in unit tests without FastMCP, an MCP client, or an HTTP roundtrip. See
`tests/test_mcp_tools.py`.

TOOL ROSTER:

    sleep_status    where the sleep stands, in one call      (GET  /status, trimmed)
    sleep_now       run a sleep now on the open brief         (POST /sleep)
    tend_now        redraft denied operations, cull chains    (POST /tend)
    check_now       one tick of the loop by hand              (POST /check)
    sleep_history   the last runs, newest first               (GET  /history)
    audit_sleeps    per-model numbers + the last few chains   (GET  /audit, trimmed)
    replay_draft    a draft's prompts on a candidate model    (POST /replay, trimmed)
    list_replays    which drafts can be replayed              (GET  /replays)

NOT here, on purpose: the brief and the review. Those are Memory's
(submit_brief, list_drafts, review_draft, audit_drafts) because the record
lives there; this surface is the sleep's machinery, not a second door onto
the drafts.

ASYNC, ON PURPOSE: FastMCP calls a sync tool ON the event loop. A sleep is
minutes of model calls, and every Memory read is a blocking httpx call, so a
sync tool would freeze the whole service - the viewer, /health, the tick loop's
scheduling - for as long as it ran. Every tool is a coroutine and does its
blocking work in asyncio.to_thread, exactly as the routes do.

FAILURE IS A MESSAGE: a sleep or tend already running (Busy) and a Memory
that does not answer both come back as {"ok": false, "message": ...}, never
an exception - the reader is a model deciding what to do next, and "busy, try
in a minute" is an answer it can act on where a stack trace is not.

ON THE DESCRIPTIONS: every docstring below is a prompt. It is what the main
model reads to decide which tool to reach for, and what calling it costs.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional

from mcp.server.fastmcp import FastMCP

from ..config import HippocampusConfig
from ..memory_client import MemoryClient, MemoryError
from ..replay import list_packets
from ..sleep import Busy, Hippocampus

TOOL_NAMES: tuple[str, ...] = (
    "sleep_status",
    "sleep_now",
    "tend_now",
    "check_now",
    "sleep_history",
    "audit_sleeps",
    "replay_draft",
    "list_replays",
)

BUSY = ("A sleep or tend is already running in the hippocampus. Nothing was started. "
        "It usually finishes within a few minutes; call sleep_status to see when it is "
        "done, or sleep_history for what it did.")


# ── small helpers: times a model can read, text a model does not drown in ─────
def _span(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 90:
        return f"{s}s"
    m = s // 60
    if m < 90:
        return f"{m} min"
    h, m = divmod(m, 60)
    if h < 48:
        return f"{h}h {m:02d}m"
    return f"{h // 24} days"


def _when(ts: Any, now: Optional[float] = None) -> Optional[str]:
    """'2026-09-26 03:30 (5h 12m ago)' - local time plus the distance, because
    an epoch float is a number the reader has to do arithmetic on."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if not ts:
        return None
    now = time.time() if now is None else now
    d = ts - now
    rel = "just now" if abs(d) < 1 else (f"in {_span(d)}" if d > 0 else f"{_span(d)} ago")
    return f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))} ({rel})"


def _cut(text: Any, n: int) -> Optional[str]:
    if text is None:
        return None
    t = str(text)
    return t if len(t) <= n else t[:n].rstrip() + "..."


def _sleep_line(rep: Optional[dict[str, Any]], now: float) -> Optional[dict[str, Any]]:
    if not rep:
        return None
    return {"when": _when(rep.get("finished_at"), now), "ok": not rep.get("error"),
            "error": rep.get("error"), "draft_id": rep.get("draft_id"),
            "operations": rep.get("operations"), "quiet": rep.get("quiet"),
            "catch_up": rep.get("catch_up"), "brief_id": rep.get("brief_id"),
            "brief_pulled": rep.get("brief_pulled"),
            "model_failures": len(rep.get("model_failures") or [])}


def _tend_line(rep: Optional[dict[str, Any]], now: float) -> Optional[dict[str, Any]]:
    if not rep:
        return None
    return {"when": _when(rep.get("finished_at"), now), "ok": not rep.get("error"),
            "error": rep.get("error"), "resubmitted": len(rep.get("resubmitted") or []),
            "closed": len(rep.get("closed") or []), "ended": len(rep.get("ended") or [])}


def _say_sleep(rep: dict[str, Any]) -> str:
    if rep.get("refused"):
        return "Nothing started: " + str(rep.get("message") or "a sleep cycle is already under way.")
    if rep.get("error"):
        return f"The sleep failed: {rep['error']}"
    if rep.get("draft_id"):
        return (f"Submitted draft {rep['draft_id']} with {rep.get('operations')} operation(s). "
                "It waits for your review: get_draft / review_draft on Memory.")
    if rep.get("quiet"):
        return "Nothing free to draft from: a quiet sleep. Tidied; no model was called."
    return "The sleep ran and proposed nothing (no cluster reached the threshold)."


def _say_tend(rep: dict[str, Any]) -> str:
    if rep.get("error"):
        return f"The tend failed: {rep['error']}"
    parts = []
    if rep.get("resubmitted"):
        parts.append(f"resubmitted {len(rep['resubmitted'])} redraft(s) for review")
    if rep.get("closed"):
        parts.append(f"closed {len(rep['closed'])} chain(s) that landed")
    if rep.get("ended"):
        parts.append(f"{len(rep['ended'])} chain(s) ended denied")
    if rep.get("model_failures"):
        parts.append(f"{len(rep['model_failures'])} redraft(s) the model could not write")
    return ("Tended: " + "; ".join(parts) + ".") if parts else \
        f"Nothing to tend ({rep.get('examined', 0)} reviewed draft(s) looked at)."


_CHECK_SAYS = {
    "chain_open": "A brief is open but a draft chain is still under review; the sleep waits until it lands.",
    "not_bedtime": "No brief, and it is not bedtime yet. Nothing to do.",
    "waiting_for_brief": "Past bedtime with no brief. The miss was counted; after enough of them the "
                         "hippocampus asks for one (a near-term note in Memory).",
}


class HippocampusToolImpl:
    """The actual tool implementations, callable both via FastMCP decoration
    (in production) and directly (in unit tests). Every return shape is
    JSON-serialisable."""

    def __init__(self, hippocampus: Hippocampus, memory: MemoryClient,
                 config: HippocampusConfig,
                 next_at: Optional[dict[str, Optional[float]]] = None) -> None:
        self.h = hippocampus
        self.mem = memory
        self.config = config
        # The loop writes its next tick into this dict (app.state.next_at);
        # held by reference so the status reads the live value.
        self.next_at = next_at if next_at is not None else {}

    # -- plumbing ------------------------------------------------------------
    def _memory_down(self, e: Exception, what: str) -> dict[str, Any]:
        return {"ok": False, "memory_reachable": False,
                "message": f"SerenMemory at {self.mem.base_url} is not answering ({e}). "
                           f"{what} The hippocampus holds nothing itself; it needs Memory up."}

    async def _run(self, fn: Callable[[], dict[str, Any]], what: str) -> Optional[dict[str, Any]]:
        """Memory first, then the run. A dead Memory is checked up front so it
        comes back as a message and is NOT recorded as a failed sleep: a
        failed sleep stamps last_sleep, which moves bedtime, and emits
        sleep_failed to the webhook - neither is true of 'nothing ran'."""
        try:
            await asyncio.to_thread(self.mem.health)
        except MemoryError as e:
            return self._memory_down(e, f"Nothing was {what}.")
        try:
            return await asyncio.to_thread(fn)
        except Busy:
            return None

    # -- read: where it stands ---------------------------------------------
    async def sleep_status(self) -> dict:
        """Where the sleep stands, in one call. Start here.

        Tells you: whether a sleep or tend is running right now (busy); the
        last sleep (when, ok or failed and why, the draft it submitted); the
        last tend; when bedtime is; whether a brief is waiting and whether a
        draft chain is still open (one chain at a time - a waiting brief sleeps
        only once the open chain lands); how many drafts wait for your review;
        whether the hippocampus has asked you for a brief; and the drafting
        model (configured or mechanical, up or down, and whether this service
        started it). `say` puts the state in a sentence or two.

        Cheap: reads local state plus two or three small reads from Memory.
        Changes nothing. If Memory is down it still answers, with
        memory.reachable false.
        """
        return await asyncio.to_thread(self._status)

    def _status(self) -> dict[str, Any]:
        h, cfg, now = self.h, self.config, time.time()
        due = h.next_sleep_due(now)
        lc = h.model.snapshot()
        last_check = h.state.get("last_check") or {}
        wanted = h.brief_wanted()
        tick = self.next_at.get("tick") if cfg.sleep.mode == "thread" else None
        out: dict[str, Any] = {
            "busy": h._lock.locked(),
            "mode": cfg.sleep.mode,
            "last_sleep": _sleep_line(h.state.get("last_sleep"), now),
            "last_tend": _tend_line(h.state.get("last_tend"), now),
            "last_check": ({"when": _when(last_check.get("at"), now), "status": last_check.get("status"),
                            "error": last_check.get("error")} if last_check else None),
            "bedtime": _when(due, now),
            "past_bedtime": due <= now,
            "next_tick": _when(tick, now),
            "brief_asked_for": wanted is not None,
            "brief_asked_at": _when((wanted or {}).get("asked_at"), now),
            "brief_misses": int(h.state.get("brief_misses") or 0),
            "model": {"mode": "model" if h.model_configured else "mechanical",
                      "name": cfg.model.name if h.model_configured else None,
                      "state": lc.get("state"), "managed": lc.get("managed"),
                      "started_by_us": lc.get("started_by_us"), "last_error": lc.get("last_error")},
        }
        try:
            brief = self.mem.latest_brief()
            pending = self.mem.drafts("pending", 50)
            out["brief_waiting"] = bool(brief)
            out["brief_id"] = (brief or {}).get("id")
            out["pending_review"] = len(pending)
            out["chain_open"] = bool(pending) or h._open_chain()
            out["memory"] = {"reachable": True, "url": self.mem.base_url}
        except MemoryError as e:
            out.update(brief_waiting=None, brief_id=None, pending_review=None, chain_open=None)
            out["memory"] = {"reachable": False, "url": self.mem.base_url, "error": str(e)}
        out["say"] = self._say_status(out)
        return out

    @staticmethod
    def _say_status(s: dict[str, Any]) -> str:
        parts: list[str] = []
        if s["busy"]:
            parts.append("A sleep or tend is running right now.")
        if not s["memory"]["reachable"]:
            parts.append(f"SerenMemory ({s['memory']['url']}) is not answering; nothing can sleep until it does.")
        ls = s["last_sleep"]
        if ls:
            parts.append(f"Last sleep {ls['when']}: " + ("ok." if ls["ok"] else f"FAILED - {ls['error']}"))
        else:
            parts.append("No sleep on record yet.")
        if s["memory"]["reachable"]:
            if s["brief_waiting"] and s["chain_open"]:
                parts.append("A brief is waiting, but a draft chain is still open; it sleeps once that lands.")
            elif s["brief_waiting"]:
                parts.append("A brief is waiting; " + (f"the next tick ({s['next_tick']}) sleeps on it."
                                                       if s["mode"] == "thread" and s["next_tick"] else
                                                       "sleep_now or check_now runs it now."))
            elif s["past_bedtime"]:
                parts.append("Past bedtime with no brief" + (" - it has asked you for one." if s["brief_asked_for"]
                                                             else f" ({s['brief_misses']} miss(es) so far)."))
            else:
                parts.append(f"No brief waiting; bedtime {s['bedtime']}.")
            if s["pending_review"]:
                parts.append(f"{s['pending_review']} draft(s) wait for your review (list_drafts on Memory).")
        m = s["model"]
        if m["mode"] == "mechanical":
            parts.append("No model configured: mechanical mode.")
        elif m["last_error"]:
            parts.append(f"Model {m['state']}; last error: {m['last_error']}")
        return " ".join(parts)

    # -- act: the three things the loop does, by hand -----------------------
    async def sleep_now(self) -> dict:
        """Run a sleep NOW, on the open brief if there is one.

        You rarely need this. The loop sleeps by itself: every tick (a few
        minutes) it checks Memory for a brief and sleeps on it when no draft
        chain is open. The normal way to put the hippocampus to bed is
        submit_brief on Memory and let the next tick pick it up. Use this when
        you do not want to wait for that tick, or to sleep in `external` mode
        where nothing ticks.

        Without an open brief it sleeps unsteered - fine by hand, but the brief
        is what tells it which bits matter, so write one first if you can.
        One cycle at a time: while a draft is still under review or waiting
        for its redraft, this refuses (refused: true) and nothing starts -
        finish the review and let tend land the chain first.

        Costs: blocks until the sleep finishes - with a model, one call per
        topic cluster, usually tens of seconds to a few minutes (longer if the
        model has to be started). Submits one draft to Memory for your review.
        Returns the sleep's report, with `message` saying what came of it. If
        a sleep or tend is already running, nothing starts and you get
        busy: true.
        """
        rep = await self._run(self.h.sleep, "slept")
        if rep is None:
            return {"ok": False, "busy": True, "message": BUSY}
        if rep.get("memory_reachable") is False:
            return rep
        return {"ok": not rep.get("error") and not rep.get("refused"), "refused": bool(rep.get("refused")),
                "message": _say_sleep(rep), "report": rep}

    async def tend_now(self) -> dict:
        """Tend the open chains NOW: redraft what you denied, cull what landed.

        After you review a draft on Memory, the loop's next tick does this by
        itself. Use it when you have just denied operations with critiques and
        want the redrafts back in this session instead of in a few minutes.
        For each reviewed draft: denied operations are redrafted from your
        critique and resubmitted as the next attempt (the last permitted
        attempt goes out terminal, which is when you may edit on approve); a
        chain whose every verdict is in is closed and its brief consumed.

        Costs: one model call per denied operation (none if nothing was
        denied). Blocks until done. Returns the tend's report with `message`.
        busy: true if a sleep or tend is already running.
        """
        rep = await self._run(self.h.tend, "tended")
        if rep is None:
            return {"ok": False, "busy": True, "message": BUSY}
        if rep.get("memory_reachable") is False:
            return rep
        return {"ok": not rep.get("error"), "message": _say_tend(rep), "report": rep}

    async def check_now(self) -> dict:
        """One check of the loop, by hand: purge what was flagged, then look
        for a brief and sleep on it if one is open and no chain is.

        This is exactly what the loop does every tick, so in `thread` mode it
        only saves you the wait. Unlike sleep_now it respects the gate: no
        brief (or a chain still open) means no sleep, just the status. Past
        bedtime with no brief it counts a miss, and enough misses make the
        hippocampus ask you for a brief. Handy after submit_brief when you
        want the sleep to start now but only if the gate is really open.

        Costs: cheap when there is nothing to do; when it sleeps, the same as
        sleep_now. Returns the check's report with `message`. busy: true if a
        sleep or tend is already running.
        """
        rep = await self._run(self.h.check, "checked")
        if rep is None:
            return {"ok": False, "busy": True, "message": BUSY}
        if rep.get("memory_reachable") is False:
            return rep
        status = rep.get("status")
        if status in ("sleeping", "sleeping_on_pulled_brief") and rep.get("sleep"):
            msg = ("Slept on the small model's pulled brief. " if status == "sleeping_on_pulled_brief"
                   else f"Slept on brief {rep.get('brief_id')}. ") + _say_sleep(rep["sleep"])
            ok = not rep["sleep"].get("error")
        elif status == "error":
            msg, ok = f"The check failed: {rep.get('error')}", False
        else:
            msg, ok = _CHECK_SAYS.get(str(status), f"Checked: {status}."), True
        return {"ok": ok, "status": status, "message": msg, "report": rep}

    # -- read: what happened -----------------------------------------------
    async def sleep_history(self, limit: int = 10) -> dict:
        """The last sleeps and tends this service ran, newest first: when,
        how long, ok or the error, and what each did (draft id, operations,
        purged, quiet, catch-up; for a tend, redrafts resubmitted and chains
        closed or ended). The service keeps the last 40.

        For "did last night's sleep run, and did it work". Cheap, local, no
        Memory call. For what happened to the drafts afterwards - the
        verdicts, the critiques, what landed - use audit_sleeps.
        """
        now = time.time()
        rows = list(reversed(self.h.state.get("history") or []))
        n = max(1, min(int(limit or 10), 40))
        return {"count": len(rows),
                "entries": [{"when": _when(r.get("finished_at"), now), **r} for r in rows[:n]]}

    async def audit_sleeps(self, limit: int = 5) -> dict:
        """How the drafting model is doing, and the last few chains in brief.

        models: one row per model that has written drafts (as stamped on each
        draft: what the server said it runs, plus the prompt version) -
        first_pass_rate (approved on the first attempt), mean_attempts to
        land, repeat_rate (a redraft denied again: the critique did not take),
        edited_on_approval, chains_ended_denied. For catching drift in the
        small model and for judging a model swap on numbers.

        chains: the `limit` most recent (default 5), each with its outcome,
        the brief that opened it, and per attempt the verdict counts and the
        critiques you gave. Operation wording is left out to keep this small:
        for a chain's full text use audit_drafts or get_draft on Memory.

        Reads Memory (the record lives there); comes back as a message if
        Memory does not answer. Changes nothing.
        """
        n = max(1, min(int(limit or 5), 50))
        try:
            a = await asyncio.to_thread(self.mem.audit, n)
        except MemoryError as e:
            return self._memory_down(e, "No audit to read.")
        now = time.time()
        return {"ok": True, "chain_count": a.get("chain_count"), "models": a.get("models") or [],
                "chains": [self._chain_line(c, now) for c in (a.get("chains") or [])[:n]]}

    @staticmethod
    def _chain_line(c: dict[str, Any], now: float) -> dict[str, Any]:
        brief = c.get("brief") or None
        attempts = []
        for d in c.get("attempts") or []:
            ops = d.get("operations") or []
            by = {s: sum(1 for op in ops if op.get("status") == s) for s in ("approved", "denied", "pending")}
            attempts.append({
                "draft_id": d.get("draft_id"), "attempt": d.get("attempt"), "terminal": d.get("terminal"),
                "status": d.get("status"), "model": (d.get("model") or {}).get("key"),
                "operations": len(ops), **by,
                "edited_on_approval": sum(1 for op in ops if op.get("edited_content")),
                "critiques": [_cut(op.get("critique"), 200) for op in ops
                              if op.get("status") == "denied" and op.get("critique")],
            })
        return {"cluster_id": c.get("cluster_id"), "outcome": c.get("outcome"),
                "started": _when(c.get("started_at"), now), "landed": c.get("landed"),
                "brief": ({"id": brief.get("id"), "summary": _cut(brief.get("summary"), 240)}
                          if brief else None),
                "attempts": attempts}

    # -- replay: the same inputs, another model -----------------------------
    async def replay_draft(self, draft_id: str, url: str = "", name: str = "",
                           timeout_seconds: Optional[int] = None,
                           extra_body: Optional[dict] = None) -> dict:
        """Replay one draft's saved prompts on a candidate model and put its
        answers beside the original's. For judging a model swap on a real
        night's input before making it.

        Every draft and redraft keeps the exact prompts it was built from (see
        list_replays for which drafts have them). This sends each one to the
        model at `url` (an OpenAI-compatible base, e.g.
        http://127.0.0.1:8091/v1; empty = the configured model), runs the
        answers through the same parser and validator a real sleep uses, and
        returns both sides: the operations each proposed, the verdicts the
        original got from you, what finally landed, and plain checks per side -
        valid operations, failed calls, invented_numbers (numbers or dates in
        the content that none of the fragments contain: a model making things
        up), overlap_landed (how close the content is to what you approved,
        0-1), seconds.

        Nothing is submitted to Memory; a replay cannot change a memory.
        Prompts are left out of the result to keep it small (the viewer's
        Replay tab shows them). `name` is the model name to send (default: the
        configured one); `extra_body` is merged into the request, e.g. to
        switch thinking off. Costs: one model call per saved prompt, so as long
        as that draft's sleep took on the candidate.
        """
        draft_id = str(draft_id or "").strip()
        if not draft_id:
            return {"ok": False, "message": "draft_id is required; list_replays shows which drafts have one."}
        if not (url or "").strip() and not self.h.model_configured:
            return {"ok": False, "message": "No model is configured here (mechanical mode), so there is nothing "
                                            "to replay on by default. Pass url= for the candidate model."}
        try:
            r = await asyncio.to_thread(self.h.replay, draft_id, url or "", name or "",
                                        extra_body, timeout_seconds)
        except KeyError as e:
            return {"ok": False, "message": str(e).strip("'\"")}

        def ops(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{"kind": op.get("kind"), "topic": op.get("topic"), "content": op.get("content"),
                     "target_core_id": op.get("target_core_id")}
                    for c in calls for op in (c.get("ops") or [])]

        cand = r.get("candidate") or {}
        orig = r.get("original") or {}
        return {
            "ok": True, "draft_id": r.get("draft_id"), "attempt": r.get("attempt"),
            "landed": r.get("landed"),
            "original": {"model": orig.get("model"), "checks": orig.get("checks"),
                         "operations": ops(orig.get("calls") or []),
                         "verdicts": [{"index": op.get("index"), "kind": op.get("kind"),
                                       "status": op.get("status"), "critique": op.get("critique"),
                                       "edited_content": op.get("edited_content")}
                                      for op in orig.get("reviewed_ops") or []]},
            "candidate": {"url": cand.get("url"), "served": cand.get("served"), "checks": cand.get("checks"),
                          "operations": ops(cand.get("calls") or []),
                          # A failed call keeps the start of what the model said, so
                          # "not the JSON asked for" can be seen, not guessed at.
                          "failures": [{"stage": c.get("stage"), "topic": c.get("topic"), "error": c.get("error"),
                                        "answer": _cut(c.get("answer"), 300)}
                                       for c in cand.get("calls") or [] if c.get("error")]},
        }

    async def list_replays(self, limit: int = 20) -> dict:
        """The drafts that can be replayed, newest first: draft id, attempt,
        how many model calls it saved, which model wrote it, and when. Pick
        one for replay_draft. Cheap and local. Drafts from before replays
        existed, or older than the last 300, have none.
        """
        n = max(1, min(int(limit or 20), 300))
        now = time.time()
        rows = await asyncio.to_thread(list_packets, self.h._state_path(), n)
        return {"count": len(rows), "entries": [{**r, "when": _when(r.get("created_at"), now)} for r in rows]}


# ═══════════════════════════════════════════════════════════════════════
#  Registration entry point
# ═══════════════════════════════════════════════════════════════════════
def register_tools(mcp: FastMCP, hippocampus: Hippocampus, memory: MemoryClient,
                   config: HippocampusConfig,
                   next_at: Optional[dict[str, Optional[float]]] = None) -> HippocampusToolImpl:
    """Attach every HippocampusToolImpl method to the given FastMCP instance
    via @mcp.tool(). Returns the impl so a caller that needs a handle (tests)
    can keep one."""
    impl = HippocampusToolImpl(hippocampus, memory, config, next_at=next_at)
    for name in TOOL_NAMES:
        mcp.tool()(getattr(impl, name))
    return impl
