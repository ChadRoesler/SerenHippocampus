"""
The /mcp mount, driven through the live app with its lifespan entered.

A shape test (is there a /mcp route) was green in SerenMemory while three
bugs made the endpoint unreachable - double-/mcp (404), the session manager's
task group never entered (500), the DNS-rebinding host check (421). Only a
request catches those, so this sends a real JSON-RPC initialize and a
tools/list, and checks the bearer the family middleware puts in front of
every route also stands in front of /mcp.

Gated on the `mcp` SDK like the tool tests.
"""
from __future__ import annotations

import json

import pytest

try:
    import mcp  # noqa: F401
    from seren_hippocampus.mcp.server import mount_mcp_routes
    from seren_hippocampus.mcp.tools import TOOL_NAMES
    _mcp_available = True
except ImportError:
    _mcp_available = False
    mount_mcp_routes = None  # type: ignore

pytestmark = pytest.mark.skipif(not _mcp_available, reason="mcp extras not installed")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from seren_hippocampus.app import create_app
from seren_hippocampus.memory_client import MemoryClient

# StreamableHTTP wants BOTH content types advertised or it 406s.
_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "seren-test", "version": "0"}}}


def _rpc(resp) -> dict:
    """The JSON-RPC reply, framed as JSON or as one SSE event."""
    text = resp.text
    if text.lstrip().startswith("{"):
        return json.loads(text)
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


@pytest.fixture
def app_with(bridge, hcfg):
    clients: list[MemoryClient] = []

    def _make(bearer: str = ""):
        cfg = hcfg.model_copy(deep=True)
        cfg.server.bearer_token = bearer
        client = MemoryClient("http://memory.test", transport=bridge)
        clients.append(client)
        return create_app(cfg, memory_client=client)
    yield _make
    for c in clients:
        c.close()


def test_the_route_serves_initialize_and_lists_the_tools(app_with):
    app = app_with()
    with TestClient(app) as tc:
        assert "/mcp" in [getattr(r, "path", None) for r in app.routes]
        r = tc.post("/mcp", json=_INIT, headers=_HEADERS)
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        assert "protocolVersion" in json.dumps(_rpc(r)["result"])
        headers = dict(_HEADERS)
        sid = r.headers.get("mcp-session-id")
        if sid:
            headers["mcp-session-id"] = sid
        tc.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
        listed = tc.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
        names = sorted(t["name"] for t in _rpc(listed)["result"]["tools"])
        assert names == sorted(TOOL_NAMES)
        assert tc.post("/mcp/mcp", json=_INIT, headers=_HEADERS).status_code == 404, "the double mount is back"


def test_the_bearer_stands_in_front_of_mcp(app_with):
    with TestClient(app_with(bearer="s3cret")) as tc:
        assert tc.post("/mcp", json=_INIT, headers=_HEADERS).status_code == 401
        ok = tc.post("/mcp", json=_INIT, headers={**_HEADERS, "Authorization": "Bearer s3cret"})
        assert ok.status_code == 200, ok.text[:300]


def test_mounting_before_the_state_is_wired_says_so():
    with pytest.raises(RuntimeError, match="hippocampus/memory/config"):
        mount_mcp_routes(FastAPI())
