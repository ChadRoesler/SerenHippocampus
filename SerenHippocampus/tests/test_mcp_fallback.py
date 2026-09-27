"""
Without the [mcp] extra - or with a mount that breaks - the hippocampus
still runs, HTTP-only. NOT gated on the SDK: this is the contract that makes
the extra optional, so it runs everywhere.

Absence is simulated by patching mount_mcp_routes to raise (the way
SerenMemory's test_mcp_fallback does), so nothing churns sys.modules under
the neighbouring tests. Where mcp is not installed at all the ImportError
fires for real and the patch is skipped.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import short
from seren_hippocampus.app import create_app
from seren_hippocampus.memory_client import MemoryClient


@pytest.mark.parametrize("exc", [ImportError("No module named 'mcp'"), RuntimeError("transport drift")])
def test_no_mcp_means_http_only_not_a_dead_service(memory, bridge, hcfg, monkeypatch, capsys, exc):
    try:
        import seren_hippocampus.mcp.server as srv

        def broken(app):
            raise exc
        monkeypatch.setattr(srv, "mount_mcp_routes", broken)
    except ImportError:
        if not isinstance(exc, ImportError):
            pytest.skip("mcp not installed: only the missing-extra path is real here")

    client = MemoryClient("http://memory.test", transport=bridge)
    app = create_app(hcfg, memory_client=client)
    with TestClient(app) as tc:
        assert "/mcp" not in [getattr(r, "path", None) for r in app.routes]
        assert tc.get("/health").json()["ok"] is True
        short(memory, "a", "t"); short(memory, "b", "t")
        assert tc.post("/sleep").json()["operations"] == 1, "the sleep runs without the MCP surface"
    client.close()
    out = capsys.readouterr().out
    assert ("HTTP-only mode" in out) if isinstance(exc, ImportError) else ("MCP mount failed" in out)
