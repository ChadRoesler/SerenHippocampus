"""
The service: a small FastAPI around the sleep.

    GET  /            service info
    GET  /health      liveness (+ whether Memory answers)
    GET  /status      last sleep, last tend, mode, intervals
    POST /sleep       run a sleep now (409 if one is running)
    POST /tend        pick up denied operations now
    /mcp              MCP server, ONLY when the [mcp] extra is installed

Bearer + request logging come from the family (Meninges, Sinew). In
sleep.mode=thread the two loops run here; in external, something else
POSTs on a schedule.

The /mcp tools are the main model's view of the sleep from a session (see
seren_hippocampus.mcp.tools). They call the same Hippocampus these routes do,
and sit behind the same bearer: the middleware wraps the mount.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from seren_meninges.auth import bearer_auth_middleware
from seren_meninges.updates import updates_payload
from seren_sinew.request_log import RequestLoggingMiddleware

from . import __version__
from .config import HippocampusConfig, load_config
from .memory_client import MemoryClient, MemoryError
from .sleep import Busy, Hippocampus
from .voice import VoiceError


def create_app(config: Optional[HippocampusConfig] = None,
               memory_client: Optional[MemoryClient] = None) -> FastAPI:
    cfg = config or load_config()
    bearer = cfg.server.resolve_bearer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg
        mem = memory_client or MemoryClient(cfg.memory.url, bearer=cfg.memory.resolve_bearer(),
                                            timeout=cfg.memory.timeout_seconds)
        app.state.memory = mem
        app.state.hippocampus = Hippocampus(cfg, mem)
        # "is there a newer seren-hippocampus" - core in Meninges, always built.
        from seren_meninges.updates import UpdateChecker
        app.state.updates = UpdateChecker(
            "seren-hippocampus",
            enabled=cfg.updates.enabled,
            index_url=cfg.updates.index_url,
            ttl_seconds=cfg.updates.check_interval_hours * 3600,
            allow_prerelease=cfg.updates.allow_prerelease,
        )
        tasks: list[asyncio.Task] = []
        app.state.next_at: dict[str, Optional[float]] = {"sleep": None, "tend": None}
        # The nudge: 'your turn' from the main model (or, on a cluster, from the
        # Observatory). It ends the wait for the next tick.
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        h0: Hippocampus = app.state.hippocampus

        async def nudged_tick():
            # The session that nudged is usually still alive, finishing its
            # answer. Wait for it to leave first: a ripple for the same event
            # is one at a time, so the next wake-up would be skipped while it
            # runs - and on a small box the main model must be out of memory
            # before the small one starts.
            await asyncio.to_thread(h0.ripple.settle, float(cfg.sleep.nudge_wait_seconds))
            try:
                await asyncio.to_thread(h0.tick)
            except Busy:
                pass
            except Exception as e:  # noqa: BLE001
                print(f"[seren-hippocampus] tick error: {e}")

        if cfg.sleep.mode == "thread":
            h0._wake = lambda: loop.call_soon_threadsafe(wake.set)
        else:
            # external mode: nothing ticks, so a nudge runs one turn itself
            h0._wake = lambda: loop.call_soon_threadsafe(lambda: tasks.append(asyncio.create_task(nudged_tick())))
        if cfg.sleep.mode == "thread":
            h: Hippocampus = app.state.hippocampus

            async def tick_loop():
                interval = cfg.sleep.heartbeat_seconds()
                print(f"[seren-hippocampus] loop active: every {interval}s close what landed and check for a "
                      "brief; redrafts " + (f"every {cfg.sleep.tend_interval_seconds}s" if cfg.sleep.tend_cycle
                                            else "only at sleep time (tend cycle off)")
                      + "; bedtime " + (f"daily at {cfg.sleep.at}" if cfg.sleep.at.strip()
                                        else f"every {cfg.sleep.interval_seconds}s"))
                while True:
                    app.state.next_at["tick"] = time.time() + interval
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=interval)
                    except asyncio.TimeoutError:
                        pass
                    if wake.is_set():
                        wake.clear()
                        await nudged_tick()
                        continue
                    try:
                        await asyncio.to_thread(h.tick)
                    except Busy:
                        pass
                    except Exception as e:  # noqa: BLE001
                        print(f"[seren-hippocampus] tick error: {e}")

            tasks.append(asyncio.create_task(tick_loop()))

        # -- Optional MCP server --
        # Mounted ONLY if the [mcp] extra is installed; a missing package falls
        # back to pure-HTTP mode without crashing. Mounted after the Hippocampus
        # and next_at exist: the tools hold references to both.
        try:
            from .mcp.server import mount_mcp_routes
            mcp_server = mount_mcp_routes(app)
        except ImportError as exc:
            mcp_server = None
            print(f"[seren-hippocampus] MCP extras not installed; HTTP-only mode ({exc})")
        except Exception as exc:  # noqa: BLE001
            mcp_server = None
            print(f"[seren-hippocampus] MCP mount failed: {exc!r} - continuing without MCP")

        # -- Run the MCP session manager's task group (Bug 2 fix) --
        # The streamable-HTTP transport keeps its anyio task group alive in
        # session_manager.run(); a mounted sub-app's own lifespan does NOT fire
        # under Starlette, so it has to be entered here or every MCP request
        # 500s with "Task group is not initialized". AsyncExitStack makes
        # HTTP-only mode a clean no-op.
        async with AsyncExitStack() as _mcp_stack:
            session_manager = getattr(mcp_server, "session_manager", None)
            if session_manager is not None:
                await _mcp_stack.enter_async_context(session_manager.run())
                print("[seren-hippocampus] MCP session manager running")
            try:
                yield
            finally:
                for t in tasks:
                    t.cancel()
                try:
                    app.state.hippocampus.model.shutdown()
                except Exception as e:  # noqa: BLE001
                    print(f"[seren-hippocampus] model shutdown: {e}")
                if memory_client is None:
                    mem.close()

    app = FastAPI(title="SerenHippocampus", version=__version__, lifespan=lifespan,
                  description="The sleep cycle for SerenMemory: drafts what should be kept, resubmits on critique, purges what was flagged.")

    @app.get("/")
    async def root(request: Request):
        return {"service": "seren-hippocampus", "version": __version__,
                "memory": cfg.memory.url, "mode": cfg.sleep.mode,
                "model": "configured" if cfg.model.url.strip() else "mechanical",
                "updates": await updates_payload(
                    getattr(request.app.state, "updates", None),
                    distribution="seren-hippocampus",
                    installed=__version__)}

    @app.get("/health")
    async def health(request: Request):
        mem_ok = True
        try:
            await asyncio.to_thread(request.app.state.memory.health)
        except MemoryError:
            mem_ok = False
        return {"ok": True, "memory_reachable": mem_ok}

    @app.get("/status")
    async def status(request: Request):
        h: Hippocampus = request.app.state.hippocampus
        nxt = getattr(request.app.state, "next_at", {}) or {}
        return {"mode": cfg.sleep.mode, "interval_seconds": cfg.sleep.interval_seconds,
                "tend_interval_seconds": cfg.sleep.tend_interval_seconds,
                "tend_cycle": cfg.sleep.tend_cycle, "tick_seconds": cfg.sleep.heartbeat_seconds(),
                "model_configured": h.model_configured,
                "model_lifecycle": h.model.snapshot(),
                "next_sleep_at": None,                       # no timer: the brief is the gate
                "next_tick_at": nxt.get("tick") if cfg.sleep.mode == "thread" else None,
                "next_tend_at": nxt.get("tick") if cfg.sleep.mode == "thread" else None,
                "bedtime_at": h.next_sleep_due(),
                "brief_misses": int(h.state.get("brief_misses") or 0),
                "last_check": h.state.get("last_check"),
                "sleep_at": cfg.sleep.at or None,
                "catch_up_next": h.is_catch_up(),
                "sleep_due_at": h.next_sleep_due(),
                "brief_wanted": h.brief_wanted() is not None,
                "brief_requested_at": (h.brief_wanted() or {}).get("asked_at"),
                "webhook": bool(cfg.notify.webhook_url),
                "last_sleep": h.state.get("last_sleep"), "last_tend": h.state.get("last_tend")}

    @app.post("/check")
    async def check(request: Request):
        """One check by hand: is there a brief? Sleeps on it if so."""
        h: Hippocampus = request.app.state.hippocampus
        try:
            return await asyncio.to_thread(h.check)
        except Busy as e:
            raise HTTPException(409, str(e))

    @app.post("/nudge")
    async def nudge(request: Request):
        """'Your turn.' Returns at once; the hippocampus runs a turn of its
        loop now - sleeps on a waiting brief, redrafts what was denied,
        closes what landed - instead of at the next tick. Sent by the main
        model after a brief or a review, or by an Observatory once the main
        model's server is out of the way."""
        h: Hippocampus = request.app.state.hippocampus
        return h.nudge()

    @app.get("/events")
    async def events(request: Request):
        """What happened, newest first: drafts submitted, sleeps failed,
        chains ended, purges. The same events go to notify.webhook_url when
        one is configured; each says whether that delivery worked."""
        h: Hippocampus = request.app.state.hippocampus
        rows = list(h.state.get("events") or [])
        return {"count": len(rows), "webhook": bool(cfg.notify.webhook_url),
                "entries": list(reversed(rows))}

    @app.get("/history")
    async def history(request: Request):
        """The last sleeps and tends this service ran, newest first."""
        h: Hippocampus = request.app.state.hippocampus
        rows = list(h.state.get("history") or [])
        return {"count": len(rows), "entries": list(reversed(rows))}

    @app.get("/queue")
    async def queue(request: Request):
        """What is waiting for review, read from SerenMemory. The queue lives
        there; this is a window onto it so the viewer can show it without a
        second token. A Memory that does not answer is reported, not raised."""
        mem: MemoryClient = request.app.state.memory
        try:
            rows = await asyncio.to_thread(mem.drafts, "pending", 50)
        except MemoryError as e:
            return {"count": 0, "drafts": [], "error": str(e)}
        return {"count": len(rows), "drafts": rows}

    @app.get("/audit")
    async def audit(request: Request, limit: int = 20):
        """Every sleep's chain end to end - brief, attempts, verdicts,
        critiques, edits, what landed - and how each model did, read from
        SerenMemory (the record lives there). For catching drift and for
        judging a model swap on numbers. Reported, not raised, when Memory
        does not answer."""
        mem: MemoryClient = request.app.state.memory
        try:
            return await asyncio.to_thread(mem.audit, limit)
        except MemoryError as e:
            return {"chains": [], "models": [], "error": str(e)}

    @app.get("/replays")
    async def replays(request: Request, limit: int = 50):
        """The drafts that can be replayed: each one's saved model calls."""
        h: Hippocampus = request.app.state.hippocampus
        from .replay import list_packets
        rows = list_packets(h._state_path(), limit)
        return {"count": len(rows), "entries": rows}

    @app.post("/replay")
    async def replay(request: Request, body: dict = Body(...)):
        """Send a draft's saved prompts to a candidate model and put the two
        side by side: {"draft_id": ..., "url": "http://host:port/v1", "name":
        optional, "extra_body": optional, "timeout_seconds": optional}. An
        empty url replays on the configured model. Nothing reaches Memory."""
        h: Hippocampus = request.app.state.hippocampus
        draft_id = str((body or {}).get("draft_id") or "")
        if not draft_id:
            raise HTTPException(400, "'draft_id' is required")
        try:
            return await asyncio.to_thread(
                h.replay, draft_id, str(body.get("url") or ""), str(body.get("name") or ""),
                body.get("extra_body"), body.get("timeout_seconds"))
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))

    # -- the voice card (seren_hippocampus.voice): the MCP tools' HTTP twins,
    # for a harness that is not MCP. 404 while it is off - opt in.
    def _voice(request: Request):
        v = request.app.state.hippocampus.voice
        if not v.enabled:
            raise HTTPException(404, "the voice card is off (opt in: voice.enabled: true)")
        return v

    @app.get("/voice")
    async def voice_get(request: Request):
        cur = await asyncio.to_thread(_voice(request).current)
        return {"version": 0, "text": None} if not cur else cur

    @app.put("/voice")
    async def voice_put(request: Request, body: dict = Body(...)):
        """{"text": the whole card, "why": what changed}. A new version; the
        old ones are kept. The same text is not a new version."""
        v = _voice(request)
        try:
            return await asyncio.to_thread(v.set, str((body or {}).get("text") or ""), str(body.get("why") or ""))
        except VoiceError as e:
            raise HTTPException(400, str(e))

    @app.get("/voice/history")
    async def voice_history(request: Request, limit: int = 10):
        rows = await asyncio.to_thread(_voice(request).history, max(1, min(int(limit), 200)))
        return {"count": len(rows), "versions": rows}

    @app.get("/viewer")
    async def viewer():
        """The window for whoever runs this and did not build it: did the sleep
        run, what is waiting for review, and when it broke, what broke. Public
        route; its API calls carry the token via the shell's 🔑 modal."""
        from pathlib import Path
        from fastapi.responses import HTMLResponse
        from seren_meninges.viewer import render_from_dir
        html = render_from_dir(
            Path(__file__).resolve().parent / "viewer" / "ui",
            title="SerenHippocampus",
            brand="Seren<b>Hippocampus</b> · the sleep",
            subtitle=f"v{__version__} · drafts what should be kept, resubmits on critique, purges what was flagged",
            accent="#c9a0dc",
        )
        return HTMLResponse(html)

    @app.post("/sleep")
    async def sleep_now(request: Request, without_brief: bool = False):
        """Start a sleep by hand. With no brief waiting it asks for one
        (asked_for_brief: true) and the check sleeps on it when it arrives;
        ?without_brief=true drafts unsteered, now."""
        h: Hippocampus = request.app.state.hippocampus
        try:
            return await asyncio.to_thread(h.sleep, not without_brief)
        except Busy as e:
            raise HTTPException(409, str(e))

    @app.post("/tend")
    async def tend_now(request: Request):
        h: Hippocampus = request.app.state.hippocampus
        try:
            return await asyncio.to_thread(h.tend)
        except Busy as e:
            raise HTTPException(409, str(e))

    app.add_middleware(bearer_auth_middleware(bearer))                                   # inner
    app.add_middleware(RequestLoggingMiddleware, service_name="seren-hippocampus",     # outer
                       env_prefix="SEREN_HIPPOCAMPUS")
    return app
