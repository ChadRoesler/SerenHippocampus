"""
The hippocampus snapshots what it keeps (seren_sinew.stores): the state file,
the voice card with every version, the replay packets - not its logs, and
not its own snapshots. Pinned here:

- GET /stores declares it; POST /stores/snapshot copies it
- logs are left out, and a second snapshot does not contain the first
- the backup block is read from the yaml and is known to the unknown-key check
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from seren_hippocampus.app import create_app
from seren_hippocampus.config import load_config, warn_unknown_settings
from seren_hippocampus.memory_client import MemoryClient


def test_it_keeps_its_state_and_not_its_logs(memory, bridge, hcfg):
    state_dir = Path(hcfg.sleep.state_path).parent
    hcfg.backup.every_hours = 0
    hcfg.voice.enabled = True
    app = create_app(hcfg, memory_client=MemoryClient("http://memory.test", transport=bridge))
    with TestClient(app) as tc:
        tc.app.state.hippocampus.voice.set("the card", why="a test")
        (state_dir / "model.log").write_text("noise", encoding="utf-8")
        (state_dir / "replays").mkdir(exist_ok=True)
        (state_dir / "replays" / "abc.json").write_text("{}", encoding="utf-8")
        tc.post("/tend")                                         # writes state.json
        d = tc.get("/stores").json()
        assert d["service"] == "seren-hippocampus" and d["stores"][0]["kind"] == "dir"
        first = tc.post("/stores/snapshot", json={"reason": "by hand"}).json()["snapshot"]
        second = tc.post("/stores/snapshot").json()["snapshot"]
        root = Path(second["path"])
        files = sorted(p.relative_to(root / "raw" / "state").as_posix() for p in (root / "raw" / "state").rglob("*") if p.is_file())
        assert "voice.json" in files and "replays/abc.json" in files and "state.json" in files
        assert "model.log" not in files, "logs are not what a hippocampus is"
        assert not any(f.startswith("backups/") for f in files), "a snapshot does not hold the snapshots"
        size = tc.get("/stores").json()["stores"][0]["bytes"]
        tc.post("/stores/snapshot")
        assert tc.get("/stores").json()["stores"][0]["bytes"] == size, "and the store's size does not count them"
        man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        assert man["version"] and man["voice_card_version"] == 1 and first["id"] != second["id"]
        assert tc.get("/stores/snapshots").json()["count"] == 3


def test_the_backup_block_is_read_and_known(tmp_path):
    y = tmp_path / "h.yaml"
    y.write_text("backup:\n  every_hours: 6\n  keep_daily: 3\n", encoding="utf-8")
    cfg = load_config(str(y))
    assert cfg.backup.every_hours == 6 and cfg.backup.keep_daily == 3 and cfg.backup.enabled is True
    assert warn_unknown_settings({"backup": {"every_hours": 6}}) == []
    assert any("backup.nope" in w for w in warn_unknown_settings({"backup": {"nope": 1}}))


def test_a_rehearsal_reads_the_copy_back(memory, bridge, hcfg):
    """A restore's dry run (seren_sinew.stores): the state file and the
    replay packets parse and the voice card is the version the manifest says;
    a packet that does not parse fails it."""
    state_dir = Path(hcfg.sleep.state_path).parent
    hcfg.backup.every_hours = 0
    hcfg.voice.enabled = True
    app = create_app(hcfg, memory_client=MemoryClient("http://memory.test", transport=bridge))
    with TestClient(app) as tc:
        tc.app.state.hippocampus.voice.set("the card", why="a test")
        tc.app.state.hippocampus.voice.set("the card, again", why="a test")
        (state_dir / "replays").mkdir(exist_ok=True)
        (state_dir / "replays" / "abc.json").write_text("{}", encoding="utf-8")
        tc.post("/tend")
        sid = tc.post("/stores/snapshot").json()["snapshot"]["id"]
        rep = tc.post(f"/stores/snapshots/{sid}/rehearse").json()
        assert rep["ok"] and rep["dry_run"] and rep["live_store_touched"] is False, rep
        assert rep["check"]["state_file"] and rep["check"]["voice_card_version"] == 2 and rep["check"]["replays"] == 1
        (state_dir / "replays" / "bad.json").write_text("{not json", encoding="utf-8")
        sid = tc.post("/stores/snapshot").json()["snapshot"]["id"]
        rep = tc.post(f"/stores/snapshots/{sid}/rehearse").json()
        assert rep["ok"] is False and rep["problems"][0].startswith("replays/bad.json"), rep
