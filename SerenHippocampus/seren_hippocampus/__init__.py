"""
SerenHippocampus - the sleep cycle for SerenMemory, split out.

The small model (the Inside Out workers) reads the short-terms the daily
brief steered it to and writes a DOCKET of operations on long-term - new
cores, evidence attached to existing cores, supersessions, verbatim keeps -
which the main model reviews per operation through SerenMemory. Denied
operations come back here with a critique; the tend loop redrafts and
resubmits. Flagged memories are purged at sleep, with a tombstone.

This service holds no store. Everything durable lives in SerenMemory; the
hippocampus is judgement and scheduling, reachable over HTTP and (with the
[mcp] extra) as MCP tools.
"""
from __future__ import annotations

try:
    from ._version import __version__
except Exception:  # noqa: BLE001 - source checkout without a generated _version.py
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
