"""
The model shelf: bringing a model the Lyceum trained into the hippocampus.

The Lyceum trains on the Spark; the small model runs wherever Lodestar keeps
`llama` (the Nano). Bringing one over is three asks, none of which carries
the file through here:

  1. the LYCEUM, for a short-lived link to the finished GGUF (/api/link)
  2. LODESTAR, to have the node that runs the model fetch that link into the
     folder it serves from (the Observatory's llama shelf, passed through)
  3. LODESTAR again, to point the model at it (the env file's LLAMA_MODEL,
     the line it replaced kept as LLAMA_MODEL_PREVIOUS - going back is one
     more switch)

Switching takes the sleep's lock, so a model is never changed under a
draft: a running sleep or tend makes it a refusal (Busy), not a wait. The
server this hippocampus started is stopped first, and one somebody else has
up is restarted so it serves the new file; the next sleep starts on it
either way. A switch is an event (model_switched) and in the audit the
drafts it makes carry the new model's name, so the before and after can be
judged on numbers, the way the audit was built to.

Needs Lodestar (model.lifecycle.lodestar_url) and a lyceum block for the
first ask; a box that runs its own model from model.lifecycle.model_path
changes that line instead.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Optional

import httpx


class ShelfError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Shelf:
    def __init__(self, cfg: Any, hippocampus: Any, transport: Optional[httpx.BaseTransport] = None):
        self._cfg = cfg
        self._h = hippocampus
        self._transport = transport

    # -- where things are ------------------------------------------------------
    @property
    def lyceum_configured(self) -> bool:
        return bool(self._cfg.lyceum.url.strip())

    @property
    def lodestar_configured(self) -> bool:
        lc = self._cfg.model.lifecycle
        return bool(lc.lodestar_url.strip() and lc.lodestar_service.strip())

    def _call(self, base: str, token: str, method: str, path: str, body: Any = None,
              timeout: float = 60.0) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as c:
                r = c.request(method, base.rstrip("/") + path, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise ShelfError(f"{base} did not answer ({type(e).__name__})", 502) from e
        try:
            data = r.json()
        except ValueError:
            data = {"detail": r.text[:300]}
        if r.status_code >= 400:
            why = data.get("detail") or data.get("error") if isinstance(data, dict) else data
            raise ShelfError(str(why or f"{r.status_code}"), r.status_code if r.status_code < 500 else 502)
        return data

    def _lyceum(self, method: str, path: str, body: Any = None) -> Any:
        if not self.lyceum_configured:
            raise ShelfError("no Lyceum is configured (lyceum.url)", 404)
        ly = self._cfg.lyceum
        return self._call(ly.url, ly.resolve_bearer(), method, path, body, ly.timeout_seconds)

    def _lodestar(self, method: str, verb: str, body: Any = None) -> Any:
        if not self.lodestar_configured:
            raise ShelfError("switching models goes through Lodestar (model.lifecycle.lodestar_url); "
                             "a box that runs its own model changes model.lifecycle.model_path instead", 404)
        lc = self._cfg.model.lifecycle
        return self._call(lc.lodestar_url, lc.resolve_lodestar_token(), method,
                          f"/api/v1/service/{lc.lodestar_service.strip()}/{verb}", body)

    # -- reads -----------------------------------------------------------------
    def lyceum_models(self) -> list[dict[str, Any]]:
        """Every finished Lyceum training that made a GGUF, newest first."""
        out = []
        for j in self._lyceum("GET", "/api/jobs").get("jobs") or []:
            st = j.get("status") or {}
            if st.get("state") != "done":
                continue
            try:
                full = self._lyceum("GET", f"/api/jobs/{j['id']}?log_bytes=2000")
            except ShelfError:
                continue
            for a in full.get("artifacts") or []:
                if str(a.get("path", "")).endswith(".gguf"):
                    out.append({"job_id": j["id"], "name": j.get("name"), "base_model": j.get("base_model"),
                                "dataset": j.get("dataset"), "created_at": j.get("created_at"),
                                "file": PurePosixPath(a["path"]).name, "path": a["path"], "bytes": a.get("bytes"),
                                "loss": st.get("loss"), "eval_loss": st.get("eval_loss"),
                                "quant": ((full.get("spec") or {}).get("export") or {}).get("quant")})
        return out

    def serving(self) -> dict[str, Any]:
        """What the model box has and which one it serves: {node, models,
        models_dir, serving}. Lodestar's /models route predates the shelf and
        nests the node's answer under "models"; it is lifted out here."""
        got = self._lodestar("GET", "models")
        inner = got.get("models")
        if isinstance(inner, dict):
            got = {"node": got.get("node"), **inner}
        return got

    def fetch_status(self) -> dict[str, Any]:
        return self._lodestar("GET", "models/fetch")

    # -- the asks ----------------------------------------------------------------
    def fetch(self, job_id: str, path: str = "") -> dict[str, Any]:
        """Have the model box fetch a Lyceum training's GGUF."""
        if not path:
            mine = [m for m in self.lyceum_models() if m["job_id"] == job_id]
            if not mine:
                raise ShelfError(f"no finished GGUF from Lyceum training {job_id!r}", 404)
            path = mine[0]["path"]
        name = PurePosixPath(path).name
        link = self._lyceum("POST", "/api/link", {"kind": "out", "id": job_id, "path": path})
        url = self._cfg.lyceum.url.rstrip("/") + str(link.get("url") or "")
        got = self._lodestar("POST", "models/fetch", {"url": url, "name": name})
        self._h._emit("model_fetch_started", name=name, lyceum_job=job_id, node=got.get("node"))
        return got

    def use(self, name: str) -> dict[str, Any]:
        """Serve `name` from now on. Refused while a sleep or tend runs."""
        from .sleep import Busy
        if not self._h._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is running; switch when it is done")
        try:
            was = (self.serving().get("serving") or {}).get("current") or ""
            stopped = self._h.model.stop_now(f"switching to {name}")
            got = self._lodestar("PUT", "model", {"name": name})
            restarted = False
            try:
                st = (self._lodestar("GET", "status").get("status") or {})
                if st.get("running"):
                    self._lodestar("POST", "restart")
                    restarted = True
            except ShelfError:
                pass                     # the next sleep starts it on the new file either way
            self._h._emit("model_switched", name=name, previous=PurePosixPath(was).name if was else "",
                          node=got.get("node"), restarted=restarted)
            self._h._save_state()
            self._h._log(f"model switched to {name}" + (f" (was {PurePosixPath(was).name})" if was else ""))
            return {**got, "stopped_ours": stopped, "restarted": restarted,
                    "say": f"{name} is the small model now" + ("; it was restarted to load it." if restarted
                                                              else "; the next sleep starts it.")}
        finally:
            self._h._lock.release()

    def forget(self, name: str) -> dict[str, Any]:
        return self._lodestar("DELETE", f"models/{name}")
