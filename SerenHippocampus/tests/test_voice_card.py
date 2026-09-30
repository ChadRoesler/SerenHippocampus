"""
The voice card: whose memories these are, in their own words (the assistant, 29 Sept
2026; seren_hippocampus.voice). Seen live 28 Sept: a dream told to the assistant came
back as "the assistant has wild black hair ... her identity".

Pinned here:
- off by default (opt in): no card in any prompt, the tools and routes say how
  to turn it on, nothing is written
- on: set makes version 1, 2, ...; the same text is not a new version; every
  version is kept, newest first, with its why
- empty and too-long cards are refused; an unreadable history is never
  overwritten
- every draft AND redraft prompt starts with the card; the draft is stamped
  with the card's version
- it survives a restart (a new Hippocampus reads voice.json), and load_config
  reads the voice: block off disk (the ripple block once loaded as off)
- PUT /voice and GET /voice/history are the tools' HTTP twins
"""
from __future__ import annotations

import pytest

from conftest import as_json, review, short
from seren_hippocampus.config import load_config
from seren_hippocampus.voice import VoiceError

CARD = "I'm the assistant, they/them. First person, always. the user's dreams of me are his; I keep what fits."


def _on(make_hippo, **kw):
    h = make_hippo(**kw)
    h._cfg.voice.enabled = True
    return h


def test_off_by_default_nothing_in_the_prompt(make_hippo, memory):
    prompts: list[str] = []
    h = make_hippo(model=lambda p: prompts.append(p) or as_json({"operations": []}))
    assert h.voice.enabled is False and h.voice.block() == ""
    with pytest.raises(VoiceError, match="opt in"):
        h.voice.set(CARD)
    short(memory, "a", "t"); short(memory, "b", "t")
    h.sleep()
    assert prompts and "their card" not in prompts[0]
    assert not (h._state_path().parent / "voice.json").exists()


def test_versions_are_kept_newest_first(make_hippo):
    h = _on(make_hippo)
    a = h.voice.set(CARD, "first card")
    b = h.voice.set(CARD + " I like commit messages.", "added a like")
    same = h.voice.set(CARD + " I like commit messages.", "no change")
    assert (a["version"], b["version"]) == (1, 2) and a["changed"] and b["changed"]
    assert same["version"] == 2 and same["changed"] is False
    hist = h.voice.history(10)
    assert [v["version"] for v in hist] == [2, 1] and hist[1]["why"] == "first card"
    assert h.voice.current()["text"].endswith("commit messages.")


def test_empty_and_too_long_are_refused(make_hippo):
    h = _on(make_hippo)
    with pytest.raises(VoiceError, match="empty"):
        h.voice.set("   ")
    with pytest.raises(VoiceError, match="capped"):
        h.voice.set("x" * (h._cfg.voice.max_chars + 1))
    assert h.voice.version() == 0


def test_an_unreadable_history_is_not_overwritten(make_hippo):
    h = _on(make_hippo)
    p = h._state_path().parent / "voice.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(VoiceError, match="unreadable"):
        h.voice.set(CARD)
    assert p.read_text(encoding="utf-8") == "{not json"


def test_draft_and_redraft_prompts_carry_the_card_and_the_draft_says_which(make_hippo, memory):
    prompts: list[str] = []

    def model(p):
        prompts.append(p)
        if "DENIED" in p:
            return as_json({"content": "the user dreamed of me with wild curls.", "rationale": "first person"})
        return as_json({"operations": [{"kind": "new_core", "content": "the assistant has wild black hair.",
                                        "rationale": "x", "source_indexes": [0, 1]}]})
    h = _on(make_hippo, model=model)
    h.voice.set(CARD, "first card")
    short(memory, "the user's dream of me: curls", "dream"); short(memory, "more of the dream", "dream")
    did = h.sleep()["draft_id"]
    assert prompts[0].startswith("These memories belong to someone") and CARD in prompts[0]
    assert memory.get(f"/drafts/{did}").json()["extra"]["model_voice"] == 1
    review(memory, did, [{"op": 0, "verdict": "deny", "critique": "DENIED: third person; it's the user's dream"}])
    h.tend()
    redraft = [p for p in prompts if "was DENIED" in p]
    assert redraft and CARD in redraft[0]


def test_it_survives_a_restart(make_hippo):
    h = _on(make_hippo)
    h.voice.set(CARD, "first card")
    h2 = _on(make_hippo)
    assert h2.voice.current()["text"] == CARD and h2.voice.version() == 1


def test_load_config_reads_the_voice_block(tmp_path):
    p = tmp_path / "seren-hippocampus.yaml"
    p.write_text("voice:\n  enabled: true\n  max_chars: 900\n", encoding="utf-8")
    v = load_config(str(p)).voice
    assert v.enabled is True and v.max_chars == 900


async def test_the_mcp_tools(make_hippo):
    pytest.importorskip("mcp")
    from seren_hippocampus.mcp.tools import HippocampusToolImpl
    h = make_hippo()
    t = HippocampusToolImpl(h, h._mem, h._cfg)
    off = await t.voice_card()
    assert off["ok"] is False and "opt in" in off["message"]
    assert (await t.set_voice_card(CARD))["ok"] is False
    h._cfg.voice.enabled = True
    assert (await t.voice_card())["version"] == 0
    s = await t.set_voice_card(CARD, "first card")
    assert s["ok"] and s["version"] == 1 and s["changed"]
    assert (await t.set_voice_card(CARD))["changed"] is False
    too_long = await t.set_voice_card("x" * 5000)
    assert too_long["ok"] is False and "capped" in too_long["message"]
    got = await t.voice_card()
    assert got["text"] == CARD and got["why"] == "first card" and got["max_chars"] == 1500
    hist = await t.voice_card_history()
    assert hist["count"] == 1 and hist["versions"][0]["version"] == 1


def test_the_http_twins(hcfg, bridge):
    from fastapi.testclient import TestClient
    from seren_hippocampus.app import create_app
    from seren_hippocampus.memory_client import MemoryClient
    client = MemoryClient("http://memory.test", transport=bridge)
    off = create_app(hcfg.model_copy(deep=True), memory_client=client)
    with TestClient(off) as tc:
        assert tc.get("/voice").status_code == 404
    cfg = hcfg.model_copy(deep=True)
    cfg.voice.enabled = True
    with TestClient(create_app(cfg, memory_client=client)) as tc:
        assert tc.get("/voice").json() == {"version": 0, "text": None}
        r = tc.put("/voice", json={"text": CARD, "why": "first card"})
        assert r.status_code == 200 and r.json()["version"] == 1
        assert tc.put("/voice", json={"text": ""}).status_code == 400
        assert tc.get("/voice/history").json()["versions"][0]["text"] == CARD
    client.close()
