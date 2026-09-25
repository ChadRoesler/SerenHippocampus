"""
seren_hippocampus.model_lifecycle
════════════════════════════════════════════════════════════════════════

The small model is on only while the hippocampus needs it.

WHY: the floor is a Nano, and the dev box is a 4 GB card. A drafting model
that sits loaded all day holds VRAM the main model, the embedder, or a game
needs, for a job that runs a few minutes a night and after a review. So when
`model.lifecycle.manage` is on, the hippocampus:

- starts the server when a sleep or a redraft actually needs it, and waits
  until it answers its health check;
- keeps it warm for `keep_warm_seconds` after the last call, so a review
  followed by a redraft reuses it;
- stops it after that, on the next tick;
- NEVER stops a server it did not start. If the server already answered when
  the hippocampus looked, someone started it for something else.

Two ways to start it. A COMMAND (`start`, optional `stop`): the hippocampus
launches the process itself, which is what a desktop with llama-server wants.
Or the node's OBSERVATORY (`observatory_url` + `observatory_service`): the
model server is a registered service and the Observatory starts and stops it,
which is what a Jetson wants.

A model that will not come up raises ModelUnavailable. The sleep that needed
it fails and keeps its brief for the next check; it never falls back to a
mechanical copy of the fragments, which is what a missing model used to do
without saying so. After a failed start the next attempt waits
`retry_after_seconds`, so a broken command is not relaunched every tick.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx


class ModelUnavailable(RuntimeError):
    """The small model is configured and could not be reached or started."""


class ModelLifecycle:
    def __init__(self, cfg, log: Callable[[str], None], emit: Callable[..., None],
                 transport: Optional[httpx.BaseTransport] = None,
                 log_dir: Optional[Path] = None) -> None:
        self._model = cfg                     # ModelConfig
        self._lc = cfg.lifecycle
        self._log = log
        self._emit = emit
        self._transport = transport
        self._log_dir = log_dir
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self.started_by_us = False
        self.state = "unknown"                # unknown | up | down | starting | failed
        self.last_used = 0.0
        self.last_failure = 0.0
        self.last_error = ""

    # ── the health check ──────────────────────────────────────────────────
    @property
    def health_url(self) -> str:
        if self._lc.health_url.strip():
            return self._lc.health_url.strip()
        base = self._model.url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/health"               # llama.cpp, vLLM and friends answer here

    def healthy(self) -> bool:
        try:
            with httpx.Client(timeout=3.0, transport=self._transport) as c:
                r = c.get(self.health_url)
            return r.status_code == 200
        except Exception:  # noqa: BLE001 - down is an answer
            return False

    @property
    def managed(self) -> bool:
        return bool(self._lc.manage and (self._lc.start.strip() or
                                         (self._lc.observatory_url.strip() and self._lc.observatory_service.strip())))

    # ── up / down ─────────────────────────────────────────────────────────
    def ensure_up(self) -> None:
        """Ready to take a call, or ModelUnavailable. Cheap when it is up."""
        with self._lock:
            self.last_used = time.time()
            if self.healthy():
                if self.state != "up":
                    self.state = "up"
                return
            if not self.managed:
                self.state = "down"
                raise ModelUnavailable(f"the model at {self._model.url} is not answering "
                                       f"({self.health_url}) and model.lifecycle.manage is off")
            wait = self._lc.retry_after_seconds - (time.time() - self.last_failure)
            if self.last_failure and wait > 0:
                raise ModelUnavailable(f"the model failed to start {int(time.time() - self.last_failure)}s ago "
                                       f"({self.last_error}); next attempt in {int(wait)}s")
            self._start()

    def _start(self) -> None:
        self.state = "starting"
        t0 = time.time()
        how = "observatory" if not self._lc.start.strip() else "command"
        self._log(f"starting the model ({how})")
        try:
            if how == "command":
                self._start_command()
            else:
                self._observatory("start")
        except Exception as e:  # noqa: BLE001
            self._fail(f"start failed: {type(e).__name__}: {e}")
        deadline = t0 + self._lc.ready_timeout_seconds
        while time.time() < deadline:
            if self.healthy():
                self.state = "up"
                self.started_by_us = True
                self.last_failure = 0.0
                self.last_error = ""
                self._emit("model_started", how=how, seconds=round(time.time() - t0, 1), url=self._model.url)
                self._log(f"model up after {round(time.time() - t0, 1)}s")
                return
            if self._proc is not None and self._proc.poll() is not None:
                self._fail(f"the start command exited with code {self._proc.returncode} before the model answered")
            time.sleep(self._lc.poll_seconds)
        self._stop_quietly()
        self._fail(f"the model did not answer {self.health_url} within {self._lc.ready_timeout_seconds}s")

    def _fail(self, why: str) -> None:
        self.state = "failed"
        self.last_failure = time.time()
        self.last_error = why
        self._emit("model_start_failed", error=why)
        self._log(why)
        raise ModelUnavailable(why)

    def _start_command(self) -> None:
        cmd: Any = self._lc.start.strip()
        if not IS_WINDOWS:
            cmd = shlex.split(cmd)
        out = subprocess.DEVNULL
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            out = open(self._log_dir / "model.log", "ab")        # noqa: SIM115 - handed to the child
        kw: dict[str, Any] = {"stdout": out, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                              "cwd": self._lc.cwd or None}
        if IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        self._proc = subprocess.Popen(cmd, **kw)

    def _observatory(self, verb: str) -> None:
        url = (self._lc.observatory_url.rstrip("/")
               + f"/api/v1/service/{self._lc.observatory_service}/{verb}")
        headers = {}
        tok = self._lc.resolve_observatory_token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        with httpx.Client(timeout=60.0, transport=self._transport) as c:
            r = c.post(url, headers=headers)
        r.raise_for_status()
        body = r.json() if r.content else {}
        if isinstance(body, dict) and body.get("ok") is False:
            raise RuntimeError(str(body.get("error") or body.get("stderr") or body)[:200])

    def release(self) -> None:
        with self._lock:
            self.last_used = time.time()

    def maybe_stop(self, now: Optional[float] = None) -> bool:
        """On the tick: stop the server this hippocampus started once it has
        been idle for keep_warm_seconds. True when it stopped something."""
        with self._lock:
            if not (self.managed and self.started_by_us):
                return False
            now = time.time() if now is None else now
            if now - self.last_used < self._lc.keep_warm_seconds:
                return False
            self._stop_quietly()
            self.started_by_us = False
            self.state = "down"
            self._emit("model_stopped", idle_seconds=round(now - self.last_used))
            self._log("model stopped (idle)")
            return True

    def _stop_quietly(self) -> None:
        try:
            if self._lc.stop.strip():
                cmd: Any = self._lc.stop.strip()
                if not IS_WINDOWS:
                    cmd = shlex.split(cmd)
                subprocess.run(cmd, cwd=self._lc.cwd or None, timeout=60,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=10)
            elif self._lc.observatory_url.strip() and self._lc.observatory_service.strip():
                self._observatory("stop")
        except Exception as e:  # noqa: BLE001 - a stop that fails is logged, never raised
            self._log(f"stopping the model failed: {e}")
        finally:
            self._proc = None

    def shutdown(self) -> None:
        """The hippocampus is going away: do not leave a model it started."""
        with self._lock:
            if self.started_by_us:
                self._stop_quietly()
                self.started_by_us = False
                self.state = "down"

    def snapshot(self) -> dict[str, Any]:
        return {"managed": self.managed, "state": self.state, "started_by_us": self.started_by_us,
                "health_url": self.health_url, "last_used": self.last_used or None,
                "keep_warm_seconds": self._lc.keep_warm_seconds, "last_error": self.last_error or None}


IS_WINDOWS = sys.platform.startswith("win") or os.name == "nt"
