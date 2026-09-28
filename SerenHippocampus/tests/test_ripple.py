"""
The ripple: at bedtime the hippocampus asks the model, it doesn't wait to be
remembered (Chad, 27 Sept 2026; punch-list row hip-ripple).

- off by default: an event is recorded and nobody is rippled
- script: the command runs with {message} / {draft_id} filled per argument and
  the event in the environment, never through a shell; the outcome is on the event
- one at a time per event: a second bedtime while the first ripple still runs is skipped
- a program that is not on the PATH fails the ripple and names the account, and
  never fails the thing that emitted the event
- endpoint: the event and its message are POSTed with the bearer
- only the configured events ripple; an unknown type is refused at load
"""
from __future__ import annotations

import json
import sys
import time

import httpx
import pytest

from seren_hippocampus.config import RippleConfig

RECORDER = """
import json, os, sys, time
out = sys.argv[1]
if len(sys.argv) > 3 and sys.argv[3] == "slow":
    time.sleep(3)
with open(out, "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[2:], "event": os.environ.get("SEREN_RIPPLE_EVENT"),
               "payload": json.loads(os.environ.get("SEREN_RIPPLE_JSON") or "{}")}, f)
"""


def _script(tmp_path, *extra):
    rec = tmp_path / "recorder.py"
    rec.write_text(RECORDER, encoding="utf-8")
    out = tmp_path / "rippled.json"
    return [sys.executable, str(rec), str(out), "{message}", *extra], out


def _wait_for(path, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.1)
    raise AssertionError(f"the ripple never wrote {path}")


def test_off_by_default(make_hippo):
    h = make_hippo()
    h._emit("brief_requested", due_at=1.0)
    assert "ripple" not in h.state["events"][-1]


def test_a_script_ripple_fills_the_message_and_passes_the_event(make_hippo, tmp_path):
    cmd, out = _script(tmp_path, "{draft_id}")
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", cmd
    h._emit("draft_submitted", draft_id="d" * 32, operations=3)
    ev = h.state["events"][-1]
    assert ev["ripple"]["ok"] is True and ev["ripple"]["type"] == "script", ev
    got = _wait_for(out)
    message, draft = got["argv"]
    assert "d" * 32 in message and draft == "d" * 32
    assert "review_draft" in message, "the default wording says what to do"
    assert got["event"] == "draft_submitted" and got["payload"]["operations"] == 3
    h.ripple.wait()


def test_the_bedtime_message_asks_for_a_brief(make_hippo, tmp_path):
    cmd, out = _script(tmp_path)
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", cmd
    h._emit("brief_requested", due_at=1.0)
    got = _wait_for(out)
    assert "bedtime" in got["argv"][0].lower() and "submit_brief" in got["argv"][0]
    h.ripple.wait()


def test_one_ripple_at_a_time_per_event(make_hippo, tmp_path):
    cmd, out = _script(tmp_path, "slow")
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", cmd
    h._emit("brief_requested", due_at=1.0)
    h._emit("brief_requested", due_at=1.0)
    first, second = h.state["events"][-2]["ripple"], h.state["events"][-1]["ripple"]
    assert first["ok"] is True
    assert second["ok"] is False and "still running" in second["skipped"]
    h.ripple.wait()


def test_a_missing_program_fails_the_ripple_not_the_emitter(make_hippo):
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", ["definitely-not-a-real-cli-4f2a", "{message}"]
    h._emit("brief_requested", due_at=1.0)
    ripple = h.state["events"][-1]["ripple"]
    assert ripple["ok"] is False and "not on the PATH" in ripple["error"]


def test_only_the_configured_events_ripple(make_hippo, tmp_path):
    cmd, out = _script(tmp_path)
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", cmd
    h._emit("purged", ids=["x"])
    assert "ripple" not in h.state["events"][-1]


def test_an_endpoint_ripple_posts_the_event_and_message(make_hippo):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})
    h = make_hippo()
    h.ripple._transport = httpx.MockTransport(handler)
    h._cfg.ripple.type, h._cfg.ripple.url, h._cfg.ripple.bearer_token = "endpoint", "http://lodestar.local/ripple", "t0k"
    h._emit("brief_requested", due_at=1.0)
    assert h.state["events"][-1]["ripple"] == {"type": "endpoint", "ok": True, "status": 200}
    path, auth, body = seen[0]
    assert path == "/ripple" and auth == "Bearer t0k"
    assert body["event"] == "brief_requested" and "submit_brief" in body["message"]


def test_an_unknown_ripple_type_is_refused():
    with pytest.raises(ValueError):
        RippleConfig(type="carrier-pigeon")
    assert RippleConfig(type="off").type == "" and RippleConfig(type="Script").type == "script"


def test_a_privileged_hippocampus_with_no_run_as_refuses(make_hippo, tmp_path, monkeypatch):
    """Root or LocalSystem with no run_as must not run the config's command as
    itself: a config edit is not a root shell."""
    from seren_sinew import runas
    monkeypatch.setattr(runas, "whoami", lambda: ("root", True))
    cmd, out = _script(tmp_path)
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command = "script", cmd
    h._emit("brief_requested", due_at=1.0)
    ripple = h.state["events"][-1]["ripple"]
    assert ripple["ok"] is False and "run_as is empty" in ripple["error"]
    assert not out.exists(), "nothing ran"


def test_run_as_the_same_account_runs(make_hippo, tmp_path):
    from seren_sinew import runas
    me, _ = runas.whoami()
    cmd, out = _script(tmp_path)
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command, h._cfg.ripple.run_as = "script", cmd, me
    h._emit("brief_requested", due_at=1.0)
    assert h.state["events"][-1]["ripple"]["ok"] is True
    _wait_for(out)
    h.ripple.wait()


def test_an_endpoint_that_refuses_keeps_its_reason(make_hippo):
    """A 409 from the model box ('not logged on', 'not set up') must reach the
    event as that sentence, not as a bare '409 Conflict'."""
    import httpx as _h

    def handler(request):
        return _h.Response(409, json={"ok": False, "error": "chad is not logged on to this box"})
    h = make_hippo()
    h.ripple._transport = _h.MockTransport(handler)
    h._cfg.ripple.type, h._cfg.ripple.url = "endpoint", "http://desktop:7777/api/v1/system/ripple"
    h._emit("brief_requested", due_at=1.0)
    r = h.state["events"][-1]["ripple"]
    assert r["ok"] is False and r["status"] == 409 and "not logged on" in r["error"]


def test_a_stdin_ripple_sends_the_message_on_stdin(make_hippo, tmp_path):
    """ripple.stdin: the message goes on stdin - how it crosses `ssh desktop
    claude -p` when there is neither Lodestar nor an Observatory."""
    rec = tmp_path / "stdin_rec.py"
    out = tmp_path / "stdin.json"
    rec.write_text("import json, sys\n"
                   "open(sys.argv[1], 'w').write(json.dumps({'stdin': sys.stdin.read(), 'argv': sys.argv[2:]}))\n",
                   encoding="utf-8")
    h = make_hippo()
    h._cfg.ripple.type, h._cfg.ripple.command, h._cfg.ripple.stdin = "script", [sys.executable, str(rec), str(out)], True
    h._emit("brief_requested", due_at=1.0)
    got = _wait_for(out)
    assert "bedtime" in got["stdin"].lower() and got["argv"] == []
    h.ripple.wait()
