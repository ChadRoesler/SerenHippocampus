"""
seren_hippocampus.ripple
════════════════════════════════════════════════════════════════════════

The hippocampus asks; the model answers.

THE NAME: in a sleeping brain, sharp-wave ripples are the bursts the
hippocampus fires to reach the cortex - the channel the day's memories travel
on to be kept. Here a ripple is the hippocampus reaching the main model: at
bedtime ("write me a brief?"), and when a draft or a redraft waits for review.
Chad named it, 28 Sept 2026; it was "the poke" until then.

WHY: the sleep cycle should start from the hippocampus - "it's bedtime, what
do you want me to do?" - not from the main model remembering to write a brief
(Chad, 27 Sept 2026, punch-list row hip-ripple). A brief is where the running
jokes get marked "keep", and a chore that gets skipped is how the bit gets
lost. So when a configured event fires, the hippocampus sends a ripple:

- `script`: run a command, e.g. `claude -p "{message}"`. The woken model has
  its memory tools and answers through them (submit_brief, review_draft).
  Chad's idea: any harness with a CLI can be woken this way.
- `endpoint`: POST the event and its message to a URL (Lodestar, a bridge).

Built into the hippocampus, not tied to one harness: tying it to one would be
a mandate. The webhook in `notify` stays what it was - a record of what
happened - and the ripple is the question.

The command is an argument LIST, never a shell string: placeholders
({message}, {event}, {draft_id}) are filled per argument, and the event's data
also rides in the environment (SEREN_RIPPLE_EVENT, SEREN_RIPPLE_MESSAGE,
SEREN_RIPPLE_JSON). Memory content is never pasted into a command line.

A script runs in the background with a timeout, one at a time per event kind,
so a slow model never stalls the tick and two bedtimes never stack. The
command runs as the hippocampus's own account: a CLI whose login lives in a
user profile (claude, for one) needs the service to run as that user.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from seren_sinew.ripple import RippleRunner, fill


# Said first, on every default message. Seen live 30 Sept 2026: of four runs
# woken in one evening, two wrote 'he is not in the room' and one signed its
# letter 'reviewed with Chad in the room' - nobody was. A woken run has no way
# to know, so it is told.
WOKEN = ("You were woken by the hippocampus, not by a person: nobody is at the keyboard or reading along, "
         "so do not address anyone or say anyone was present. ")

DEFAULT_MESSAGES = {
    "brief_requested": (
        "It's bedtime. The hippocampus wants tonight's brief before it sleeps: a summary of what mattered, "
        "promote_hints for what to keep (short phrases copied word for word from the memories), and "
        "noise_hints for the one-offs. Write it with submit_brief on your memory server. If it is not "
        "a good time, or nothing is worth keeping tonight, say so in the summary."),
    "draft_submitted": (
        "The hippocampus drafted long-term memories from tonight's sleep (draft {draft_id}). Review them "
        "on your memory server: get_draft, then review_draft with a verdict per operation and a specific "
        "critique for anything you deny."),
    "tend_resubmitted": (
        "The hippocampus redrafted operations you denied. Review the new attempt on your memory server: "
        "list_drafts, get_draft, then review_draft."),
    "brief_unmatched": (
        "The hippocampus slept on your brief, but none of its promote_hints matched a memory, so it kept "
        "nothing. The brief is set aside, not used up. Write a new one with submit_brief: hints that are "
        "short phrases lifted from the memories themselves, or their topic tags."),
}


class Ripple:
    def __init__(self, cfg, log: Callable[[str], None],
                 transport: Optional[httpx.BaseTransport] = None,
                 log_dir: Optional[Path] = None) -> None:
        self._cfg = cfg                       # RippleConfig
        self._log = log
        self._transport = transport
        self._log_dir = log_dir
        # The shared runner (seren_sinew.ripple): the same one the Observatory
        # and Lodestar use. One instance, so "one at a time per event" holds
        # across fires; its settings are refreshed from the config on each.
        self._runner = RippleRunner()

    @property
    def enabled(self) -> bool:
        return self._cfg.type in ("script", "endpoint")

    def wants(self, event: str) -> bool:
        return self.enabled and event in (self._cfg.events or [])

    def message(self, ev: dict[str, Any]) -> str:
        kind = str(ev.get("event") or "")
        custom = (self._cfg.messages or {}).get(kind)
        tmpl = custom or DEFAULT_MESSAGES.get(kind) or f"The hippocampus says: {kind}."
        if not custom:
            tmpl = WOKEN + tmpl                            # an operator's own wording is left as written
        return fill(tmpl, {"message": "", "event": kind, "draft_id": str(ev.get("draft_id") or "")})

    def fire(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Ripple for one event. Never raises: the outcome comes back as a dict
        the caller records on the event."""
        msg = self.message(ev)
        try:
            if self._cfg.type == "endpoint":
                return self._post(ev, msg)
            return self._run(ev, msg)
        except Exception as e:  # noqa: BLE001 - a ripple never fails a sleep
            self._log(f"ripple {ev.get('event')} failed: {e}")
            return {"type": self._cfg.type, "ok": False, "error": f"{type(e).__name__}: {e}"}

    # ── endpoint: an Observatory, or Lodestar routing it ──────────────────
    def _post(self, ev: dict[str, Any], msg: str) -> dict[str, Any]:
        if not self._cfg.url:
            return {"type": "endpoint", "ok": False, "error": "ripple.url is empty"}
        headers = {"Content-Type": "application/json"}
        tok = self._cfg.resolve_bearer()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        with httpx.Client(timeout=30.0, transport=self._transport) as c:
            r = c.post(self._cfg.url, json={"service": "seren-hippocampus", "message": msg, **ev},
                       headers=headers)
        if r.is_success:
            return {"type": "endpoint", "ok": True, "status": r.status_code}
        # Keep the receiver's reason ("chad is not logged on", "ripple is not
        # set up on this node") - a bare "409 Conflict" says nothing to fix.
        try:
            why = (r.json() or {}).get("error") or r.text
        except ValueError:
            why = r.text
        return {"type": "endpoint", "ok": False, "status": r.status_code, "error": str(why)[:400]}

    # ── script: the model is on this box ──────────────────────────────────
    def argv(self, ev: dict[str, Any], msg: str) -> list[str]:
        self._sync()
        return self._runner.argv(str(ev.get("event") or ""), msg, str(ev.get("draft_id") or ""))

    def _sync(self) -> None:
        c, r = self._cfg, self._runner
        r.command, r.run_as, r.cwd = c.command, c.run_as or "", c.cwd or ""
        r.timeout_seconds, r.stdin = float(c.timeout_seconds), bool(c.stdin)
        r.log_path = (self._log_dir / "ripple.log") if self._log_dir is not None else None

    def _run(self, ev: dict[str, Any], msg: str) -> dict[str, Any]:
        self._sync()
        kind = str(ev.get("event") or "")
        _, answer = self._runner.run(kind, msg, draft_id=str(ev.get("draft_id") or ""), payload=ev)
        if answer.get("ok"):
            self._log(f"ripple {kind}: started as {self._cfg.run_as or 'this account'} (pid {answer.get('pid')})")
        elif answer.get("error"):
            self._log(f"ripple {kind}: {answer['error']}")
        return {"type": "script", **answer}

    def wait(self, timeout: float = 30.0) -> None:
        """Tests and shutdown: wait for ripples still running."""
        self._runner.wait(timeout)
