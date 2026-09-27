"""
The hippocampus's only view of the world: SerenMemory's HTTP API.

Synchronous on purpose (the sleep runs in a worker thread, like the
in-process consolidator did), with an injectable transport so the tests can
run the real SerenMemory app in-process behind it and exercise the contract
rather than a mock of it.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx


class MemoryError(RuntimeError):
    """SerenMemory answered with an error, or did not answer."""


class MemoryClient:
    def __init__(self, base_url: str, bearer: str = "", timeout: float = 30.0,
                 transport: Optional[httpx.BaseTransport] = None):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        self._c = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                               timeout=timeout, transport=transport)
        self.base_url = base_url.rstrip("/")

    def close(self) -> None:
        self._c.close()

    # -- plumbing ----------------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise MemoryError(f"{method} {path}: {e}") from e
        if r.status_code >= 400:
            raise MemoryError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    # -- reads ---------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._req("GET", "/health")

    def shorts(self, limit: int = 500) -> list[dict[str, Any]]:
        return list(self._req("GET", "/short", params={"limit": limit}).get("entries") or [])

    def open_briefs(self, limit: int = 50) -> list[dict[str, Any]]:
        got = self._req("GET", "/brief", params={"limit": limit})
        rows = got.get("entries") if isinstance(got, dict) else got
        return list(rows or [])

    def latest_brief(self) -> Optional[dict[str, Any]]:
        got = self._req("GET", "/brief", params={"limit": 1})
        rows = got.get("entries") if isinstance(got, dict) else got
        return rows[0] if rows else None

    def search_cores(self, query: str, n: int = 5) -> list[dict[str, Any]]:
        """Live cores near a query: long tier only, no satellites, no history."""
        got = self._req("POST", "/search", json={
            "query": query[:2000], "n_results": max(1, min(int(n), 50)),
            "include_short": False, "include_near": False, "include_long": True,
        })
        return [h for h in (got.get("hits") or []) if h.get("tier") == "long"]

    def drafts(self, status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return list(self._req("GET", "/drafts", params=params).get("entries") or [])

    def audit(self, limit: int = 20) -> dict[str, Any]:
        """Every chain end to end and the numbers per model (Memory's /audit)."""
        return dict(self._req("GET", "/audit", params={"limit": limit}) or {})

    def draft_chain(self, draft_id: str) -> list[dict[str, Any]]:
        return list(self._req("GET", f"/drafts/{draft_id}/chain").get("attempts") or [])

    # -- writes --------------------------------------------------------------------
    def submit_brief(self, summary: str, promote_hints: list[str], noise_hints: list[str],
                     completed_intents: list[str]) -> dict[str, Any]:
        return self._req("POST", "/brief", json={
            "summary": summary, "promote_hints": promote_hints,
            "noise_hints": noise_hints, "completed_intents": completed_intents,
        })

    def submit_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        return self._req("POST", "/drafts", json=draft)

    # ── near-term intents: how the hippocampus leaves the model a note ──
    def add_near(self, intent: str, topic: Optional[str] = None, trigger_type: str = "always",
                 trigger_value: Optional[str] = None, expires_at: Optional[float] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"intent": intent, "topic": topic, "trigger_type": trigger_type,
                                "trigger_value": trigger_value}
        if expires_at is not None:
            body["expires_at"] = expires_at
        return self._req("POST", "/near", json=body)

    def list_near(self, include_completed: bool = False) -> list[dict[str, Any]]:
        got = self._req("GET", "/near", params={"include_completed": str(include_completed).lower()})
        return list(got.get("entries") or []) if isinstance(got, dict) else []

    def complete_near(self, entry_id: str) -> dict[str, Any]:
        return self._req("POST", f"/near/{entry_id}/complete")

    def delete_near(self, entry_id: str) -> dict[str, Any]:
        return self._req("DELETE", f"/near/{entry_id}")

    def consume_brief(self, brief_id: str, draft_id: Optional[str] = None) -> dict[str, Any]:
        return self._req("POST", f"/brief/{brief_id}/consume", json={"draft_id": draft_id})

    def close_draft(self, draft_id: str) -> dict[str, Any]:
        return self._req("POST", f"/drafts/{draft_id}/close")

    def tidy(self, *, age_out: bool, near: bool, sweep: bool, purge: bool) -> dict[str, Any]:
        return self._req("POST", "/tidy", json={"age_out": age_out, "near": near,
                                                "sweep": sweep, "purge": purge})
