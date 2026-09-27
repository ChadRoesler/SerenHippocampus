"""
seren_hippocampus.mcp.server
════════════════════════════

Wires the FastMCP server INTO the existing FastAPI app at /mcp.

Same process, same port. The MCP tools call the Hippocampus and MemoryClient
the routes use - no HTTP round-trip back to ourselves. Mounted at /mcp by
default; override via SEREN_HIPPOCAMPUS_MCP_MOUNT.

This is a near-exact sibling of seren_memory.mcp.server, seren_loci.mcp.server
and seren_margin.mcp.server - the same three transport footguns bite any
FastMCP-into-FastAPI mount, so the same three fixes apply. Kept parallel on
purpose: fix one, fix all four.

SDK COMPATIBILITY: the modern transport is streamable HTTP
(`streamable_http_app()`); older versions used SSE (`sse_app()`). We try the
newer first and fall back. The extra pins mcp<2: 2.0 removed FastMCP.

AUTH AND THE BIND ADDRESS: mounting MCP does NOT widen the listener, and the
family's bearer middleware sits outside the mount, so /mcp wants the same
bearer as /sleep. That matters more here than for a read-only surface: these
tools start sleeps, which spend model time and submit drafts. Beyond loopback
with no token the service refuses to start, as every Seren service does.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def mount_mcp_routes(app: FastAPI):
    """Mount the SerenHippocampus MCP server onto an existing FastAPI app.

    Called from seren_hippocampus.app at startup IF the [mcp] extra is
    installed (the import gate in app.py catches ImportError when `mcp` isn't
    available).

    Reads app.state.hippocampus, app.state.memory and app.state.config (set by
    the lifespan handler) to wire the tools to live objects, and
    app.state.next_at for when the loop ticks next. Returns the FastMCP
    instance; the caller MUST enter `mcp.session_manager.run()` for the app's
    lifetime (the streamable-HTTP transport's task group lives there) - see
    app.py's lifespan.
    """
    # Imported here, not at module top, so an import failure of `mcp` bubbles up
    # to app.py's try/except (HTTP-only fallback) rather than crashing load.
    from mcp.server.fastmcp import FastMCP

    from .tools import register_tools

    mount_path = os.environ.get("SEREN_HIPPOCAMPUS_MCP_MOUNT", "/mcp").rstrip("/")
    if not mount_path.startswith("/"):
        mount_path = "/" + mount_path

    hippocampus = getattr(app.state, "hippocampus", None)
    memory = getattr(app.state, "memory", None)
    config = getattr(app.state, "config", None)
    if hippocampus is None or memory is None or config is None:
        raise RuntimeError(
            "mount_mcp_routes called before app.state.hippocampus/memory/config "
            "were set. Mount inside the lifespan handler after the Hippocampus "
            "is constructed."
        )

    mcp = FastMCP("seren-hippocampus")
    register_tools(mcp, hippocampus, memory, config,
                   next_at=getattr(app.state, "next_at", None))

    # -- Bug 1: the double-/mcp footgun --
    # streamable_http_app()/sse_app() serve at settings.streamable_http_path,
    # which DEFAULTS TO "/mcp". If we then mount at "/mcp", the real endpoint
    # lands at "/mcp/mcp" and "/mcp" itself 404s. Push the sub-app's own path to
    # root so mount("/mcp", ...) resolves to exactly "/mcp". hasattr-guarded for
    # older (sse-only) SDKs.
    if hasattr(mcp.settings, "streamable_http_path"):
        mcp.settings.streamable_http_path = "/"

    # -- Bug 3: DNS-rebinding host check vs cross-host access --
    # FastMCP ships DNS-rebinding protection defaulting to localhost-only, which
    # silently 421s a client reaching the box by hostname. The hippocampus binds
    # to loopback by default, so the common case is unaffected either way; the
    # check defaults OFF to match Memory, Loci and Margin rather than have one
    # service in the family behave differently under the same env. Re-arm with
    # SEREN_HIPPOCAMPUS_MCP_ALLOWED_HOSTS (comma-sep) and optionally
    # SEREN_HIPPOCAMPUS_MCP_ALLOWED_ORIGINS.
    if hasattr(mcp.settings, "transport_security"):
        _apply_transport_security(mcp)

    asgi_app = _resolve_transport_app(mcp)
    app.mount(mount_path, asgi_app)
    logger.info("[seren-hippocampus] MCP server mounted at %s (%d tools)",
                mount_path, _count_tools(mcp))

    # -- Bug 2: the mounted sub-app's lifespan never runs --
    # Returned so app.py's lifespan can run the session manager's task group;
    # Starlette does NOT fire a mounted sub-app's lifespan, and the session
    # manager raises "Task group is not initialized" on first request otherwise.
    return mcp


def _apply_transport_security(mcp) -> None:
    """Configure FastMCP's DNS-rebinding host check from env, defaulting OFF
    (family-consistent posture). Local import so SDKs without the module don't
    break import of this file."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception as exc:  # noqa: BLE001
        logger.info("[seren-hippocampus] transport_security module unavailable (%s); "
                    "leaving SDK default in place", exc)
        return

    def _split(name: str) -> list[str]:
        return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]

    allowed_hosts = _split("SEREN_HIPPOCAMPUS_MCP_ALLOWED_HOSTS")
    allowed_origins = _split("SEREN_HIPPOCAMPUS_MCP_ALLOWED_ORIGINS")

    if allowed_hosts or allowed_origins:
        if not allowed_origins:
            allowed_origins = [f"http://{h}" for h in allowed_hosts] + \
                              [f"https://{h}" for h in allowed_hosts]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
        logger.info("[seren-hippocampus] MCP host check ON; allowed_hosts=%s",
                    allowed_hosts)
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        logger.info("[seren-hippocampus] MCP host check OFF; set "
                    "SEREN_HIPPOCAMPUS_MCP_ALLOWED_HOSTS to enable an allowlist")


def _resolve_transport_app(mcp) -> object:
    """Return an ASGI app for the MCP HTTP transport, tolerating SDK drift.
    Tries streamable_http (current) then sse (legacy)."""
    for attr in ("streamable_http_app", "sse_app"):
        factory = getattr(mcp, attr, None)
        if callable(factory):
            logger.info("[seren-hippocampus] MCP transport: %s", attr)
            return factory()
    try:
        import mcp as _mcp_pkg
        version = getattr(_mcp_pkg, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        version = "unknown"
    raise RuntimeError(
        f"mcp SDK version {version} exposes neither streamable_http_app nor "
        "sse_app on FastMCP - cannot mount HTTP transport. Try "
        "`pip install -U 'mcp<2'` or pin a known-good version in extras."
    )


def _count_tools(mcp) -> int:
    """Best-effort tool count for the startup log line. The SDK doesn't promise
    a stable attribute; try a couple of likely shapes, fall back to 0."""
    for attr in ("_tools", "tools", "_tool_manager"):
        obj = getattr(mcp, attr, None)
        if obj is None:
            continue
        if hasattr(obj, "list_tools"):
            try:
                return len(list(obj.list_tools()))
            except Exception:  # noqa: BLE001
                continue
        if isinstance(obj, dict):
            return len(obj)
    return 0
