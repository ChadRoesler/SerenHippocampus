"""
One id for everything a sleep sends (design note, 25 Oct 2026).

Every call the hippocampus made to Memory reached it with no request id, so
Memory minted a new one per call and a sleep could not be followed through
Memory's log. A sleep, a tend and a tick now each bind one id, and every call
inside carries it; a sleep started by a request keeps that request's id.
"""
from __future__ import annotations

import json

import httpx
from seren_sinew.request_log import REQUEST_ID_HEADER, request_id_scope

from conftest import short
from seren_hippocampus.memory_client import MemoryClient
from seren_hippocampus.sleep import Hippocampus


def _recorded(bridge, hcfg):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(REQUEST_ID_HEADER))
        return bridge.handle_request(request)
    h = Hippocampus(hcfg, MemoryClient("http://memory.test", transport=httpx.MockTransport(handler)),
                    log=lambda m: None)
    return h, seen


def test_every_call_one_sleep_makes_carries_one_id(memory, bridge, hcfg):
    short(memory, "the lantern on the porch", "lantern")
    short(memory, "the lantern again", "lantern")
    h, seen = _recorded(bridge, hcfg)
    rep = h.sleep()
    assert rep["draft_id"] and len(seen) > 3
    assert len(set(seen)) == 1 and seen[0].startswith("sleep-")
    seen.clear()
    h.tick()
    assert len(set(seen)) == 1 and seen[0].startswith("tick-"), "a tick is one id, its tend and check inside it"


def test_a_sleep_a_request_started_keeps_the_requests_id(memory, bridge, hcfg):
    short(memory, "a", "t")
    h, seen = _recorded(bridge, hcfg)
    with request_id_scope("from-post-sleep"):
        h.sleep()
    assert set(seen) == {"from-post-sleep"}


def test_the_state_file_is_written_whole(memory, bridge, hcfg, tmp_path):
    h, _ = _recorded(bridge, hcfg)
    h.sleep()
    state = tmp_path / "state.json"
    assert json.loads(state.read_text(encoding="utf-8"))["last_sleep"]
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
