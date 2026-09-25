"""
seren_hippocampus.sleep
════════════════════════════════════════════════════════════════════════

The dream-cycle, as a service that holds nothing.

SLEEP (every ~20h)
  1. purge what was flagged      Memory executes the cascade (/tidy purge)
  2. brief                       the main model's "what mattered" note if one
                                 is newer than the last sleep; otherwise pull
                                 one from recent short-terms with the small
                                 model (Oliver-Twist)
  3. docket                      group short-terms by topic tag; for each
                                 cluster worth proposing, show the small model
                                 the fragments and the nearest existing cores
                                 and ask for operations - new_core, attach,
                                 supersede, verbatim - then submit ONE docket
                                 for the sleep. Short-terms already held by a
                                 pending docket are left alone (no re-drafting
                                 the same cluster every run).
  4. tidy                        age out, maintain near-term, sweep pruned

TEND (every few minutes)
  A reviewed docket with denied operations and no later attempt in its
  chain gets those operations redrafted from the critique and resubmitted
  as attempt+1. The last permitted attempt goes out terminal=true, which is
  the reviewer's cue that edit-on-approve is now allowed. A terminal docket
  whose operations are denied ends the chain; the short-terms stay for a
  later sleep.

MODEL USAGE stays minimal and stays honest: the model drafts and redrafts.
With no model configured the hippocampus runs mechanically - clusters over
the threshold become new cores by their longest entry, verbatim flags are
honoured - and never proposes attach or supersede, because deciding that
tonight's fragments are more evidence for core 41, or that yellow replaces
blue, is a judgement, and a threshold is not one.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import HippocampusConfig
from .memory_client import MemoryClient, MemoryError
from .model_lifecycle import ModelLifecycle, ModelUnavailable

OP_KINDS = ("new_core", "attach", "supersede", "verbatim")


class Busy(RuntimeError):
    """A sleep or tend is already running in this process."""


class Hippocampus:
    def __init__(self, cfg: HippocampusConfig, memory: MemoryClient, log=None,
                 notify_transport: Optional[httpx.BaseTransport] = None):
        self._cfg = cfg
        self._mem = memory
        self._log = log or (lambda m: print(f"[seren-hippocampus] {m}"))
        self._lock = threading.Lock()
        self._notify_transport = notify_transport
        self.state: dict[str, Any] = self._load_state()
        self.model = ModelLifecycle(cfg.model, log=self._log, emit=self._emit,
                                    log_dir=self._state_path().parent)

    # ── when is the next sleep due ────────────────────────────────────────
    def next_sleep_due(self, now: Optional[float] = None) -> float:
        """The moment the next sleep should start. With sleep.at, the next
        occurrence of that local time after the last sleep (today's if it has
        not passed and there was no sleep yet today); otherwise last sleep +
        interval. A due time already in the past means overdue: the caller
        sleeps after warmup_seconds instead of waiting a whole interval."""
        import datetime as _dt
        now = time.time() if now is None else now
        last = float(((self.state.get("last_sleep") or {}).get("finished_at")) or 0)
        at = (self._cfg.sleep.at or "").strip()
        if at:
            try:
                hh, mm = (int(x) for x in at.split(":", 1))
            except ValueError:
                self._log(f"sleep.at={at!r} is not HH:MM; using the interval")
            else:
                anchor = _dt.datetime.fromtimestamp(last if last else now)
                due = anchor.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due.timestamp() <= (last if last else now - 1):
                    due += _dt.timedelta(days=1)
                return due.timestamp()
        # No sleep on record (fresh install, or the state file went away): due
        # now, which the caller turns into one warm-up, not a whole interval.
        return last + self._cfg.sleep.interval_seconds if last else now

    def seconds_until_sleep(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        due = self.next_sleep_due(now)
        if due <= now:
            return float(self._cfg.sleep.warmup_seconds)      # overdue: soon, not a full interval
        return due - now

    def is_catch_up(self, now: Optional[float] = None) -> bool:
        """A sleep after a gap longer than gap_grace_intervals * interval (or
        the first sleep this service ever ran) must not trash what never had
        its chance: draft and purge, but no age-out and no sweep."""
        now = time.time() if now is None else now
        last = float(((self.state.get("last_sleep") or {}).get("finished_at")) or 0)
        if not last:
            return True
        return (now - last) > self._cfg.sleep.gap_grace_intervals * self._cfg.sleep.interval_seconds

    # ── events (the seed of "shoot me a text") ────────────────────────────
    def _emit(self, kind: str, **data: Any) -> None:
        ev = {"event": kind, "at": time.time(), **data}
        rows = list(self.state.get("events") or [])
        rows.append(ev)
        self.state["events"] = rows[-100:]
        n = self._cfg.notify
        if n.webhook_url and kind in (n.events or []):
            try:
                headers = {"Content-Type": "application/json"}
                tok = n.resolve_bearer()
                if tok:
                    headers["Authorization"] = f"Bearer {tok}"
                with httpx.Client(timeout=n.timeout_seconds, transport=self._notify_transport) as c:
                    c.post(n.webhook_url, json={"service": "seren-hippocampus", **ev}, headers=headers)
                ev["delivered"] = True
            except Exception as e:  # noqa: BLE001 - a notification never fails a sleep
                ev["delivered"] = False
                ev["delivery_error"] = f"{type(e).__name__}: {e}"
                self._log(f"webhook {kind} failed: {e}")

    # ── state (the only thing this service keeps) ─────────────────────────
    def _state_path(self) -> Path:
        return self._cfg.resolved_state_path()

    def _load_state(self) -> dict[str, Any]:
        try:
            p = self._state_path()
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return {"last_sleep": None, "last_tend": None}

    def _save_state(self) -> None:
        try:
            self._state_path().write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - the state file is a convenience, never load-bearing
            self._log(f"could not write state file: {e}")

    @property
    def model_configured(self) -> bool:
        return bool(self._cfg.model.url.strip())

    # ── the model seam (tests replace this) ───────────────────────────────
    def _call_model(self, prompt: str, max_tokens: Optional[int] = None) -> str:
        m = self._cfg.model
        if not self.model_configured:
            raise RuntimeError("no model configured")
        # Up when needed (started if lifecycle.manage is on), ModelUnavailable
        # otherwise - which the sleep turns into a failure that keeps the brief.
        self.model.ensure_up()
        url = m.url.rstrip("/") + "/chat/completions"
        payload = {"model": m.name, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens or m.max_tokens, "temperature": 0.2}
        try:
            with httpx.Client(timeout=m.timeout_seconds) as c:
                r = c.post(url, json=payload)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(f"the model at {m.url} stopped answering: {e}") from e
        finally:
            self.model.release()

    @staticmethod
    def _json_from(text: str) -> Optional[Any]:
        """Lenient: strip <think> blocks and code fences, find the first JSON
        object. A 2B model does all three of those things on a good day."""
        t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
        if t.startswith("```"):
            lines = t.splitlines()
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            t = "\n".join(lines)
        try:
            return json.loads(t)
        except ValueError:
            pass
        m = re.search(r"\{.*\}", t, flags=re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
        return None

    # ── public entry points ───────────────────────────────────────────────
    def sleep(self) -> dict[str, Any]:
        """Sleep NOW, by hand (POST /sleep, the viewer's button): on the open
        brief if there is one, without one otherwise. The loop itself never
        sleeps without a brief - see check()."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            brief = None
            try:
                brief = self._mem.latest_brief()
            except MemoryError:
                pass
            return self._sleep(brief=brief)
        finally:
            self._lock.release()

    def tend(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._tend()
        finally:
            self._lock.release()

    def check(self, now: Optional[float] = None) -> dict[str, Any]:
        """Check for a brief; sleep on it if one is there and no chain is
        open; count the misses after bedtime and ask when they add up."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._check(now)
        finally:
            self._lock.release()

    def tick(self) -> dict[str, Any]:
        """One turn of the loop: tend the open chains, check for a brief, and
        stop a model this hippocampus started once it has gone idle."""
        out = {"tend": self.tend(), "check": self.check()}
        try:
            out["model_stopped"] = self.model.maybe_stop()
        except Exception as e:  # noqa: BLE001
            self._log(f"model stop check failed: {e}")
        return out

    # ── the check: is there a brief? ──────────────────────────────────────
    def _open_chain(self) -> bool:
        """A docket is still in play: pending verdicts, or denied operations
        that tend will redraft. One chain at a time - a brief that arrives
        while one is open waits its turn."""
        if self._mem.dockets(status="pending"):
            return True
        for d in self._mem.dockets(status="reviewed"):
            denied = [op for op in (d.get("operations") or []) if op.get("status") == "denied"]
            if not denied or d.get("terminal"):
                continue                                   # tend will close it
            chain = self._mem.docket_chain(d["id"])
            if any(int(x.get("attempt", 1)) > int(d.get("attempt", 1)) for x in chain):
                continue
            return True
        return False

    def _check(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        misses = int(self.state.get("brief_misses") or 0)
        report: dict[str, Any] = {"at": now, "status": None, "brief_id": None, "misses": misses, "sleep": None}
        try:
            # Flagged memories are purged on the tick, not at the sleep: a flag is
            # a decision already made, and a month with no brief must not keep a
            # leaked key alive.
            purged = self._mem.tidy(age_out=False, near=False, sweep=False, purge=True).get("purged") or []
            if purged:
                self._log(f"purged {len(purged)} flagged memories")
                self._emit("purged", count=len(purged), ids=[t.get("id") for t in purged])

            brief = self._mem.latest_brief()
            if brief:
                report["brief_id"] = brief.get("id")
                if self._open_chain():
                    report["status"] = "chain_open"        # the brief waits for the chain to land
                else:
                    report["status"] = "sleeping"
                    self.state["brief_misses"] = 0
                    report["misses"] = 0
                    report["sleep"] = self._sleep(brief=brief)
            else:
                due = self.next_sleep_due(now)
                if now < due:
                    report["status"] = "not_bedtime"
                    self.state["brief_misses"] = 0
                    report["misses"] = 0
                else:
                    misses += 1
                    self.state["brief_misses"] = misses
                    report["misses"] = misses
                    report["status"] = "waiting_for_brief"
                    every = self._cfg.sleep.brief_check_misses
                    if self._cfg.sleep.brief_request and every > 0 and misses % every == 0:
                        self.request_brief(now)
                    if self._cfg.sleep.brief_pull and every > 0 and misses >= every and not self._open_chain():
                        pulled = self._pull_brief()
                        if pulled:
                            report["status"] = "sleeping_on_pulled_brief"
                            self.state["brief_misses"] = 0
                            report["sleep"] = self._sleep(brief=pulled)
        except (MemoryError, Exception) as e:  # noqa: BLE001 - a check records its failure and ends
            report["status"] = "error"
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"check error: {report['error']}")
        self.state["last_check"] = {k: v for k, v in report.items() if k != "sleep"}
        self._save_state()
        return report

    # ── the bedtime call ──────────────────────────────────────────────────
    def brief_wanted(self) -> Optional[dict[str, Any]]:
        """The open request, if the hippocampus has asked for a brief for the
        coming sleep and none has been answered yet."""
        req = self.state.get("brief_request")
        return req if isinstance(req, dict) and req.get("answered") is None else None

    def request_brief(self, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        """'I see you're looking tired, let's get ready for bed.' Within
        brief_lead_seconds of the next sleep, once per sleep, and only when no
        brief has arrived since the last one: leave an intent in Memory's
        near-term tier where the main model will see it, and say so on the
        events feed. The note names the time, what a brief is for, and how to
        write one. Returns the open request, or None when nothing was needed."""
        cfg = self._cfg.sleep
        if not cfg.brief_request:
            return None
        now = time.time() if now is None else now
        due = self.next_sleep_due(now)
        req = self.state.get("brief_request")
        if isinstance(req, dict) and req.get("answered") is None:
            # Already asked and still waiting: nudge the webhook again, but do
            # not leave a second note - one open loop in Memory is enough.
            req["nudges"] = int(req.get("nudges") or 0) + 1
            self._emit("brief_requested", due_at=req.get("for_sleep_due_at"), intent_id=req.get("intent_id"),
                       nudge=req["nudges"])
            self._save_state()
            return req
        if self._mem.latest_brief():
            return None                                   # the model got there first
        when = time.strftime("%H:%M", time.localtime(due)) if due > now else "soon"
        text = (f"The hippocampus wants a brief before it sleeps at {when}: what mattered since the last "
                "sleep, promote_hints for the running bits and the real lessons, noise_hints for the one-offs. "
                "submit_brief on Memory writes it; without one the small model pulls a guess from the fragments.")
        out = self._mem.add_near(text, topic="hippocampus, brief", trigger_type="always",
                                 expires_at=due + 6 * 3600)
        req = {"asked_at": now, "for_sleep_due_at": due, "intent_id": out.get("id"), "answered": None}
        self.state["brief_request"] = req
        self._emit("brief_requested", due_at=due, intent_id=out.get("id"))
        self._save_state()
        self._log(f"asked for a brief before the sleep at {when}")
        return req

    def _resolve_brief_request(self, answered: bool) -> None:
        """The sleep is here: close the open request either way, so the note
        does not linger in Memory and the next cycle can ask again."""
        req = self.state.get("brief_request")
        if not isinstance(req, dict) or req.get("answered") is not None:
            return
        req["answered"] = answered
        req["resolved_at"] = time.time()
        if req.get("intent_id"):
            try:
                self._mem.complete_near(str(req["intent_id"]))
            except Exception as e:  # noqa: BLE001
                self._log(f"could not complete the brief intent: {e}")

    # ── sleep ─────────────────────────────────────────────────────────────
    def _sleep(self, brief: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        start = time.time()
        catch_up = self.is_catch_up(start)
        report: dict[str, Any] = {"started_at": start, "purged": 0, "brief_id": None,
                                  "brief_pulled": False, "clusters": 0, "operations": 0,
                                  "docket_id": None, "held_back": 0, "tidy": {}, "error": None,
                                  "catch_up": catch_up, "quiet": False,
                                  "brief_requested": self.brief_wanted() is not None, "brief_answered": None}
        try:
            # 1. what was flagged
            purged = self._mem.tidy(age_out=False, near=False, sweep=False, purge=True).get("purged") or []
            report["purged"] = len(purged)
            if purged:
                self._log(f"purged {len(purged)} flagged memories: " +
                          ", ".join(t.get("id", "?") for t in purged))
                self._emit("purged", count=len(purged), ids=[t.get("id") for t in purged])
            if catch_up:
                last = ((self.state.get("last_sleep") or {}).get("finished_at"))
                self._log("catch-up sleep: " + ("first sleep on this store" if not last else
                          f"{round((start - last) / 3600)}h since the last one") + " - drafting, not aging out")
                self._emit("catch_up", since=last)

            # A sleep with nothing free to draft from costs no model call.
            free_now = [x for x in self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)
                        if x["id"] not in self._held_short_ids()]
            report["quiet"] = not free_now

            # 2. the brief: the gate that opened this sleep (or none, by hand)
            if report["brief_requested"]:
                answered = bool(brief and not brief.get("_pulled"))
                report["brief_answered"] = answered
                self._resolve_brief_request(answered)
            promote_hints: set[str] = set()
            noise_hints: set[str] = set()
            if brief:
                report["brief_id"] = brief.get("id")
                report["brief_pulled"] = bool(brief.get("_pulled"))
                meta = brief.get("metadata") or {}
                promote_hints = {str(h).lower() for h in (meta.get("promote_hints") or [])}
                noise_hints = {str(h).lower() for h in (meta.get("noise_hints") or [])}

            # 3. the docket
            shorts = self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)
            held = self._held_short_ids()
            free = [s for s in shorts if s["id"] not in held]
            report["held_back"] = len(shorts) - len(free)
            clusters: dict[str, list[dict[str, Any]]] = {}
            for s in free:
                clusters.setdefault((s.get("metadata") or {}).get("topic") or "_untagged", []).append(s)
            report["clusters"] = len(clusters)

            ops: list[dict[str, Any]] = []
            for topic, entries in clusters.items():
                ops += self._propose(topic, entries, promote_hints, noise_hints, brief)
            report["operations"] = len(ops)
            if ops:
                summary = self._summarise(ops)
                out = self._mem.submit_docket({"summary": summary, "operations": ops,
                                               "brief_id_used": report["brief_id"], "attempt": 1})
                report["docket_id"] = out.get("id")
                self._log(f"submitted docket {out.get('id')} with {len(ops)} operation(s)")
                # Poke the model: a note in Memory that names the docket, closed
                # when the chain lands. The event goes out for the webhook.
                note_id = None
                try:
                    note = self._mem.add_near(
                        f"A docket with {len(ops)} operation(s) waits for your review: review_docket "
                        f"{out.get('id')}. Approve or deny each operation; a denial needs a critique.",
                        topic="hippocampus, review", trigger_type="always")
                    note_id = note.get("id")
                    notes = dict(self.state.get("review_notes") or {})
                    notes[str(out.get("cluster_id") or out.get("id"))] = note_id
                    self.state["review_notes"] = notes
                except Exception as e:  # noqa: BLE001 - the note is a courtesy, the docket is the record
                    self._log(f"could not leave the review note: {e}")
                self._emit("docket_submitted", docket_id=out.get("id"), operations=len(ops),
                           kinds=sorted({op["kind"] for op in ops}), review_note_id=note_id)
            elif brief and not brief.get("_pulled") and brief.get("id"):
                # A brief with nothing to draft from: nothing to review, so the
                # brief has done its job. Consume it or the check loops forever.
                try:
                    self._mem.consume_brief(str(brief["id"]))
                    self._emit("brief_consumed", brief_id=brief["id"], docket_id=None)
                except Exception as e:  # noqa: BLE001
                    self._log(f"could not consume the brief: {e}")

            # 4. tidy - a catch-up keeps everything that has not been looked at
            report["tidy"] = self._mem.tidy(age_out=not catch_up, near=True, sweep=not catch_up, purge=False)
        except (MemoryError, Exception) as e:  # noqa: BLE001 - a sleep records its failure and ends
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"sleep error: {report['error']}")
            self._emit("sleep_failed", error=report["error"])
        report["finished_at"] = time.time()
        report["duration_seconds"] = round(report["finished_at"] - start, 2)
        self.state["last_sleep"] = report
        self._remember_run("sleep", report, operations=report["operations"], purged=report["purged"],
                           docket_id=report["docket_id"], quiet=report["quiet"], catch_up=report["catch_up"])
        if not report["error"]:
            self._emit("sleep_done", operations=report["operations"], purged=report["purged"],
                       quiet=report["quiet"], catch_up=report["catch_up"],
                       brief_answered=report["brief_answered"])
        self._save_state()
        return report

    def _close_chain(self, d: dict[str, Any], chain: list[dict[str, Any]]) -> None:
        """The cull. After the approved operations have been written to long
        by the review, close every reviewed docket in the chain, consume the
        brief that opened it, complete the review note, and say so."""
        cluster_id = d.get("cluster_id") or d["id"]
        for x in chain or [d]:
            if x.get("status") == "reviewed":
                try:
                    self._mem.close_docket(x["id"])
                except Exception as e:  # noqa: BLE001
                    self._log(f"could not close docket {x['id']}: {e}")
        brief_id = None
        for x in (chain or [d]):
            if x.get("brief_id_used"):
                brief_id = x["brief_id_used"]
                break
        if brief_id:
            try:
                self._mem.consume_brief(str(brief_id), docket_id=d["id"])
                self._emit("brief_consumed", brief_id=brief_id, docket_id=d["id"])
            except Exception as e:  # noqa: BLE001
                self._log(f"could not consume brief {brief_id}: {e}")
        notes = dict(self.state.get("review_notes") or {})
        note_id = notes.pop(str(cluster_id), None)
        if note_id:
            try:
                self._mem.complete_near(str(note_id))
            except Exception as e:  # noqa: BLE001
                self._log(f"could not complete the review note: {e}")
        self.state["review_notes"] = notes
        self.state["brief_misses"] = 0
        self._emit("chain_closed", cluster_id=cluster_id, docket_id=d["id"], brief_id=brief_id,
                   attempts=len(chain or [d]))
        self._log(f"chain {cluster_id} closed after {len(chain or [d])} attempt(s)")

    def _remember_run(self, kind: str, report: dict[str, Any], **summary: Any) -> None:
        """A bounded history for the viewer: what each run did or why it stopped."""
        rows = list(self.state.get("history") or [])
        rows.append({"kind": kind, "started_at": report.get("started_at"),
                     "finished_at": report.get("finished_at"),
                     "duration_seconds": round((report.get("finished_at") or 0) - (report.get("started_at") or 0), 2),
                     "error": report.get("error"), **summary})
        self.state["history"] = rows[-40:]

    def _held_short_ids(self) -> set[str]:
        """Short-terms already spoken for by a pending docket. Proposing them
        again every sleep is how the old consolidator made duplicate drafts."""
        held: set[str] = set()
        for d in self._mem.dockets(status="pending"):
            for op in d.get("operations") or []:
                if op.get("status", "pending") == "pending":
                    held.update(str(x) for x in (op.get("source_short_ids") or []))
        return held

    def _pull_brief(self) -> Optional[dict[str, Any]]:
        if not self.model_configured:
            return None
        rows = self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)
        if not rows:
            return None
        sample = rows[:50]
        frags = [f"[{(r.get('metadata') or {}).get('topic') or 'untagged'}] {(r.get('content') or '')[:200]}"
                 for r in sample]
        prompt = (
            "You are a memory consolidator's steering assistant. Below are recent short-term "
            "memory fragments. Produce a brief in this EXACT JSON shape (no markdown, no preamble):\n"
            '{"summary": "one short paragraph: what mattered in this period",\n'
            ' "promote_hints": ["topic phrases that should be remembered durably"],\n'
            ' "noise_hints": ["topic phrases that are noise and should not promote"],\n'
            ' "completed_intents": ["intents that look done"]}\n\nFragments:\n' + "\n".join(frags) + "\n\nJSON brief:")
        try:
            parsed = self._json_from(self._call_model(prompt, max_tokens=400))
        except Exception as e:  # noqa: BLE001
            self._log(f"brief pull failed ({e}); proceeding without a brief")
            return None
        if not isinstance(parsed, dict):
            return None
        try:
            out = self._mem.submit_brief(
                summary=str(parsed.get("summary", "")),
                promote_hints=[str(h) for h in (parsed.get("promote_hints") or [])],
                noise_hints=[str(h) for h in (parsed.get("noise_hints") or [])],
                completed_intents=[str(i) for i in (parsed.get("completed_intents") or [])])
        except MemoryError as e:
            self._log(f"brief pulled but not persisted ({e}); using it in memory")
            out = {"id": "(in-memory)"}
        return {"id": out.get("id"), "content": parsed.get("summary", ""),
                "metadata": {"created_at": time.time(),
                             "promote_hints": parsed.get("promote_hints") or [],
                             "noise_hints": parsed.get("noise_hints") or []},
                "_pulled": True}

    # ── proposing operations for one cluster ──────────────────────────────
    def _propose(self, topic: str, entries: list[dict[str, Any]],
                 promote_hints: set[str], noise_hints: set[str],
                 brief: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        real_topic = None if topic == "_untagged" else topic

        # verbatim: the person said the wording is the point - no model needed
        verbatim = [e for e in entries if (e.get("metadata") or {}).get("verbatim")]
        for e in verbatim:
            ops.append({"kind": "verbatim", "content": e.get("content") or "", "topic": real_topic,
                        "source_short_ids": [e["id"]], "evidence_count": 1,
                        "rationale": "preserved verbatim at the person's request"})
        remaining = [e for e in entries if not (e.get("metadata") or {}).get("verbatim")]
        if not remaining:
            return ops

        pinned = [e for e in remaining if (e.get("metadata") or {}).get("pinned")]
        threshold = self._cfg.sleep.promote_min_evidence
        haystack = (topic.lower() + " || " + " ".join((e.get("content") or "").lower() for e in entries))
        kept = sorted(h for h in promote_hints if h and h in haystack)
        noise = sorted(h for h in noise_hints if h and h in haystack)
        if kept:
            threshold = 1
        if noise:
            threshold = 10 ** 6
        if not pinned and len(remaining) < threshold:
            return ops

        if not self.model_configured:
            longest = max(remaining, key=lambda e: len(e.get("content") or ""))
            ops.append({"kind": "new_core", "content": longest.get("content") or "", "topic": real_topic,
                        "source_short_ids": [e["id"] for e in remaining],
                        "evidence_count": len(remaining),
                        "rationale": f"mechanical: {len(remaining)} fragment(s) on '{topic}', longest kept; no model to judge attach/supersede"})
            return ops

        candidates = self._candidates(remaining)
        drafted = self._draft_cluster(real_topic, remaining, candidates,
                                      steer=self._steer(brief, kept, noise))
        if drafted:
            ops += drafted
        else:
            longest = max(remaining, key=lambda e: len(e.get("content") or ""))
            ops.append({"kind": "new_core", "content": longest.get("content") or "", "topic": real_topic,
                        "source_short_ids": [e["id"] for e in remaining],
                        "evidence_count": len(remaining),
                        "rationale": "model draft failed; mechanical fallback (longest fragment)"})
        return ops

    def _candidates(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        query = " ".join((e.get("content") or "") for e in entries)[:1500]
        try:
            hits = self._mem.search_cores(query, n=self._cfg.sleep.candidate_cores)
        except MemoryError as e:
            self._log(f"candidate search failed ({e}); proposing without existing cores")
            return []
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            if meta.get("kind", "core") != "core" or meta.get("superseded_by"):
                continue
            out.append({"id": h["id"], "content": h.get("content") or "", "topic": meta.get("topic")})
        return out

    @staticmethod
    def _steer(brief: Optional[dict[str, Any]], kept: list[str], noise: list[str]) -> str:
        """What the main model said mattered, put in front of the drafting
        worker. The hints already move the promotion threshold; this is the
        other half of the brief's job - the summary and the matched hints go
        INTO the prompt, so the worker knows a running bit from a one-off
        instead of guessing importance from the fragments alone. It is what
        turns a one-off joke into an inside joke: the brief names the bit."""
        if not brief and not kept and not noise:
            return ""
        lines = []
        text = str((brief or {}).get("content") or "").strip()
        if text:
            who = ("pulled from the fragments by a small model" if (brief or {}).get("_pulled")
                   else "written by the main model, who was there")
            lines.append(f"The brief for this period ({who}):\n{text[:900]}")
        if kept:
            lines.append("The brief names THIS topic as worth keeping (" + "; ".join(kept[:6]) +
                         "): a running bit or a real lesson deserves a core or an attach even from few fragments.")
        if noise:
            lines.append("The brief names THIS topic as noise (" + "; ".join(noise[:6]) +
                         "): a one-off; propose nothing unless the fragments plainly outgrow that call.")
        return "\n\n".join(lines)

    def _draft_cluster(self, topic: Optional[str], entries: list[dict[str, Any]],
                       candidates: list[dict[str, Any]], steer: str = "") -> list[dict[str, Any]]:
        frag_lines = "\n".join(f"[{i}] {(e.get('content') or '')[:400]}" for i, e in enumerate(entries))
        core_lines = "\n".join(f"({c['id']}) {c['content'][:300]}" for c in candidates) or "(none)"
        steer_block = f"\n\nSteer:\n{steer}" if steer else ""
        prompt = (
            "You are a memory consolidator's drafting worker. Below are short-term memory fragments "
            "about one topic, and the existing long-term CORES closest to them. Propose operations on "
            "long-term memory. Return ONLY JSON in this shape:\n"
            '{"operations": [{"kind": "new_core|attach|supersede|verbatim", "content": "...", '
            '"target_core_id": "...", "restated_content": "...", "rationale": "...", "source_indexes": [0, 1]}]}\n'
            "Rules: attach = the fragments are more evidence for an existing core (content = a one-line "
            "episode summary; restated_content only if the core's wording should change). supersede = the "
            "fragments contradict or replace an existing core (content = the new statement). new_core = "
            "nothing existing covers it (content = one durable statement, present tense, no preamble). "
            "verbatim = only when the exact wording is the point. Use core ids exactly as given and never "
            "invent one. Every operation lists the fragment indexes it is built from. Fewer, better "
            "operations beat many. Weigh the steer when there is one: it says what mattered, which the "
            "fragments alone cannot."
            f"{steer_block}\n\n"
            f"Topic: {topic or 'untagged'}\n\nFragments:\n{frag_lines}\n\nExisting cores:\n{core_lines}\n\nJSON:")
        try:
            raw = self._call_model(prompt)
        except ModelUnavailable:
            raise                                          # the sleep fails and keeps its brief
        except Exception as e:  # noqa: BLE001
            self._log(f"draft model call failed ({e})")
            return []
        parsed = self._json_from(raw)
        if not isinstance(parsed, dict):
            return []
        return self._validate_ops(parsed.get("operations"), topic, entries, candidates)

    def _validate_ops(self, raw_ops: Any, topic: Optional[str], entries: list[dict[str, Any]],
                      candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only what the store will accept: known kinds, real core ids, real
        fragment indexes, non-empty content. A model that invents an id gets
        that operation dropped, not the whole docket."""
        core_ids = {c["id"] for c in candidates}
        out: list[dict[str, Any]] = []
        for raw in (raw_ops or []):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "")).strip().lower()
            if kind not in OP_KINDS:
                continue
            idxs = []
            for i in (raw.get("source_indexes") or []):
                try:
                    i = int(i)
                except (TypeError, ValueError):
                    continue
                if 0 <= i < len(entries):
                    idxs.append(i)
            if not idxs:
                idxs = list(range(len(entries)))
            content = str(raw.get("content") or "").strip()
            target = raw.get("target_core_id")
            if kind in ("attach", "supersede"):
                if target not in core_ids:
                    continue
            else:
                target = None
            if kind != "attach" and not content:
                continue
            op = {"kind": kind, "content": content, "topic": topic, "target_core_id": target,
                  "source_short_ids": [entries[i]["id"] for i in sorted(set(idxs))],
                  "evidence_count": len(set(idxs)),
                  "rationale": str(raw.get("rationale") or "")[:500] or None}
            if kind == "attach" and raw.get("restated_content"):
                op["restated_content"] = str(raw["restated_content"]).strip()
            out.append(op)
        return out

    @staticmethod
    def _summarise(ops: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for op in ops:
            counts[op["kind"]] = counts.get(op["kind"], 0) + 1
        parts = [f"{n} {k}" for k, n in counts.items()]
        topics = sorted({op.get("topic") or "untagged" for op in ops})
        return f"{', '.join(parts)} across {', '.join(topics)[:200]}"

    # ── tend ──────────────────────────────────────────────────────────────
    def _tend(self) -> dict[str, Any]:
        start = time.time()
        report: dict[str, Any] = {"started_at": start, "examined": 0, "resubmitted": [],
                                  "ended": [], "closed": [], "error": None}
        try:
            reviewed = self._mem.dockets(status="reviewed")
            report["examined"] = len(reviewed)
            for d in reviewed:
                denied = [op for op in (d.get("operations") or []) if op.get("status") == "denied"]
                cluster_id = d.get("cluster_id") or d["id"]
                chain = self._mem.docket_chain(d["id"])
                if any(int(x.get("attempt", 1)) > int(d.get("attempt", 1)) for x in chain):
                    continue                                   # already resubmitted
                if not denied or d.get("terminal"):
                    # The chain has landed (every verdict in, approvals applied) or
                    # is spent (the last attempt denied): cull.
                    if denied:
                        report["ended"].append(cluster_id)     # shorts stay for a later sleep
                        if cluster_id not in (self.state.get("ended_chains") or []):
                            self.state["ended_chains"] = (self.state.get("ended_chains") or [])[-200:] + [cluster_id]
                            self._emit("chain_ended", cluster_id=cluster_id, docket_id=d["id"])
                    self._close_chain(d, chain)
                    report["closed"].append(cluster_id)
                    continue
                new_ops = self._redraft(denied)
                if not new_ops:
                    continue
                attempt = int(d.get("attempt", 1)) + 1
                out = self._mem.submit_docket({
                    "summary": f"redraft of {d['id']} (attempt {attempt})",
                    "operations": new_ops, "cluster_id": cluster_id, "attempt": attempt,
                    "terminal": attempt >= self._cfg.sleep.max_attempts,
                    "previous_docket_ids": list(d.get("previous_docket_ids") or []) + [d["id"]],
                    "brief_id_used": d.get("brief_id_used"),
                })
                report["resubmitted"].append({"docket_id": out.get("id"), "cluster_id": cluster_id,
                                              "attempt": attempt, "operations": len(new_ops)})
                self._log(f"resubmitted {len(new_ops)} operation(s) for {cluster_id} as attempt {attempt}")
                self._emit("tend_resubmitted", docket_id=out.get("id"), cluster_id=cluster_id,
                           attempt=attempt, operations=len(new_ops),
                           terminal=attempt >= self._cfg.sleep.max_attempts)
        except (MemoryError, Exception) as e:  # noqa: BLE001
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"tend error: {report['error']}")
        report["finished_at"] = time.time()
        self.state["last_tend"] = report
        self._remember_run("tend", report, resubmitted=len(report["resubmitted"]), ended=len(report["ended"]),
                           closed=len(report["closed"]))
        self._save_state()
        return report

    def _redraft(self, denied: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.model_configured:
            self._log("denied operations need a model to redraft; none configured")
            return []
        shorts = {s["id"]: s for s in self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)}
        out: list[dict[str, Any]] = []
        for op in denied:
            src = [shorts[i] for i in (op.get("source_short_ids") or []) if i in shorts]
            if not src:
                self._log(f"operation {op.get('index')}: its short-terms are gone; nothing to redraft from")
                continue
            frag_lines = "\n".join(f"- {(s.get('content') or '')[:400]}" for s in src)
            prompt = (
                "You are a memory consolidator's drafting worker. A proposed operation on long-term "
                "memory was DENIED by the reviewer. Write an improved version that addresses the critique. "
                'Return ONLY JSON: {"content": "...", "restated_content": "...", "rationale": "..."}\n\n'
                f"Operation kind: {op.get('kind')}\nTopic: {op.get('topic') or 'untagged'}\n"
                f"Source fragments:\n{frag_lines}\n\nPrevious content: {op.get('content')}\n"
                f"Critique: {op.get('critique')}\n\nJSON:")
            try:
                parsed = self._json_from(self._call_model(prompt))
            except ModelUnavailable:
                raise                                      # tend records it; the chain waits for the model
            except Exception as e:  # noqa: BLE001
                self._log(f"redraft model call failed ({e})")
                continue
            if not isinstance(parsed, dict):
                continue
            content = str(parsed.get("content") or "").strip()
            if op.get("kind") != "attach" and not content:
                continue
            new = {"kind": op.get("kind"), "content": content, "topic": op.get("topic"),
                   "target_core_id": op.get("target_core_id"),
                   "source_short_ids": [s["id"] for s in src], "evidence_count": len(src),
                   "rationale": (str(parsed.get("rationale") or "")[:500] or None)}
            if op.get("kind") == "attach" and parsed.get("restated_content"):
                new["restated_content"] = str(parsed["restated_content"]).strip()
            out.append(new)
        return out
