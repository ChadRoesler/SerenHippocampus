"""
The service: a small FastAPI around the sleep.

    GET  /            service info
    GET  /health      liveness (+ whether Memory answers)
    GET  /status      last sleep, last tend, mode, intervals
    POST /sleep       run a sleep now (409 if one is running)
    POST /tend        pick up denied operations now

Bearer + request logging come from the family (Meninges, Sinew). In
sleep.mode=thread the two loops run here; in external, something else
POSTs on a schedule.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from seren_meninges.auth import bearer_auth_middleware
from seren_meninges.updates import updates_payload
from seren_sinew.request_log import RequestLoggingMiddleware

from . import __version__
from .config import HippocampusConfig, load_config
from .memory_client import MemoryClient, MemoryError
from .sleep import Busy, Hippocampus


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
        if cfg.sleep.mode == "thread":
            async def loop(name: str, interval: int, fn):
                print(f"[seren-hippocampus] {name} loop active (every {interval}s)")
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await asyncio.to_thread(fn)
                    except Busy:
                        pass
                    except Exception as e:  # noqa: BLE001
                        print(f"[seren-hippocampus] {name} error: {e}")
            tasks.append(asyncio.create_task(loop("sleep", cfg.sleep.interval_seconds, app.state.hippocampus.sleep)))
            tasks.append(asyncio.create_task(loop("tend", cfg.sleep.tend_interval_seconds, app.state.hippocampus.tend)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            if memory_client is None:
                mem.close()

    app = FastAPI(title="SerenHippocampus", version=__version__, lifespan=lifespan,
                  description="The sleep cycle for SerenMemory: drafts the docket, resubmits on critique, purges what was flagged.")

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
        return {"mode": cfg.sleep.mode, "interval_seconds": cfg.sleep.interval_seconds,
                "tend_interval_seconds": cfg.sleep.tend_interval_seconds,
                "model_configured": h.model_configured,
                "last_sleep": h.state.get("last_sleep"), "last_tend": h.state.get("last_tend")}

    @app.post("/sleep")
    async def sleep_now(request: Request):
        h: Hippocampus = request.app.state.hippocampus
        try:
            return await asyncio.to_thread(h.sleep)
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
