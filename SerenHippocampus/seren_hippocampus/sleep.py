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
  3. draft                      group short-terms by topic tag; for each
                                 cluster worth proposing, show the small model
                                 the fragments and the nearest existing cores
                                 and ask for operations - new_core, attach,
                                 supersede, verbatim - then submit ONE draft
                                 for the sleep. Short-terms already held by a
                                 pending draft are left alone (no re-drafting
                                 the same cluster every run).
  4. tidy                        age out, maintain near-term, sweep pruned -
                                 at the END of the cycle: here when the sleep
                                 drafted nothing, otherwise when its chain
                                 lands (TEND, _close_chain). Nothing ages out
                                 while a draft is under review or redraft.

TEND (every few minutes)
  A reviewed draft with denied operations and no later attempt in its
  chain gets those operations redrafted from the critique and resubmitted
  as attempt+1. The last permitted attempt goes out terminal=true, which is
  the reviewer's cue that edit-on-approve is now allowed. A terminal draft
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
from .ripple import Ripple
from .voice import VoiceCard
from . import replay as rp

OP_KINDS = ("new_core", "attach", "supersede", "verbatim")

# ── grouping memories, and matching a brief's hints to them (30 Sept 2026) ──
# Memories used to be grouped by their exact topic string, so "wren,alice,
# identity" and "wren,alice,identity,dream" never met: three copies of one dream
# landed in three piles of one, and a pile of one rarely has the evidence to be
# kept (hip-cluster-tags). They now group by shared tags - Jaccard against the
# pile's FIRST memory, so a pile cannot drift by chaining - capped so one pile
# never outgrows a small model's context.
CLUSTER_MIN_OVERLAP = 0.5
CLUSTER_MAX = 12

# A hint used to count only if its whole text appeared, letter for letter, in
# a memory: "Seren's drift tell: losing the bit" never matched "she stopped
# being in on the bit", and the biggest day of the week drafted nothing
# (hip-hint-match, 28 Sept). Now: exact text first, then one of the memory's
# tags, then - promote hints only - most of the hint's words. Noise hints stay
# strict: a noise match holds a whole pile back, and a loose one there would
# quietly drop the very memories a brief was trying to keep.
HINT_MIN_WORDS = 0.6
_HINT_SKIP = {"the", "and", "for", "with", "that", "this", "from", "about", "was", "were", "are",
              "his", "her", "their", "its", "our", "you", "your", "not", "but", "into", "out"}


def _tags(topic: Any) -> frozenset[str]:
    return frozenset(t.strip().lower() for t in str(topic or "").split(",") if t.strip())


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def hint_match(hint: str, tags: frozenset[str], haystack: str, hay_words: set[str],
               loose: bool = True) -> Optional[str]:
    """How a brief's hint matches a pile of memories: "exact" (its text is in
    them), "tag" (it is one of their tags), "words" (most of its words are
    there - loose only), or None."""
    h = (hint or "").strip().lower()
    if not h:
        return None
    if h in haystack:
        return "exact"
    if h in tags:
        return "tag"
    if not loose:
        return None
    words = [w for w in re.findall(r"[a-z0-9]+", h) if len(w) >= 3 and w not in _HINT_SKIP]
    if len(words) < 2:
        return None

    def seen(w: str) -> bool:
        if w in hay_words:
            return True
        stem = w[:max(4, len(w) - 2)]                      # "drift" finds "drifting"
        return len(w) >= 4 and any(t.startswith(stem) for t in hay_words)
    return "words" if sum(seen(w) for w in words) / len(words) >= HINT_MIN_WORDS else None


def cluster_by_tags(shorts: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """(topic, memories) piles. A memory joins the pile whose first memory
    shares the most of its tags (at least CLUSTER_MIN_OVERLAP, Jaccard), else
    starts a pile; the pile's topic is its first memory's. Untagged memories
    share one pile. Order is kept: oldest-first input, oldest-first piles."""
    piles: list[tuple[frozenset[str], str, list[dict[str, Any]]]] = []
    untagged: list[dict[str, Any]] = []
    for s in shorts:
        topic = (s.get("metadata") or {}).get("topic") or ""
        tags = _tags(topic)
        if not tags:
            untagged.append(s)
            continue
        best, best_j = None, 0.0
        for pile in piles:
            anchor, _, members = pile
            if len(members) >= CLUSTER_MAX:
                continue
            j = len(tags & anchor) / len(tags | anchor)
            if j > best_j:
                best, best_j = pile, j
        if best is not None and best_j >= CLUSTER_MIN_OVERLAP:
            best[2].append(s)
        else:
            piles.append((tags, str(topic), [s]))
    out = [(topic, members) for _, topic, members in piles]
    if untagged:
        out.append(("_untagged", untagged))
    return out


# ── what the first sleep that ran on its own showed (30 Sept 2026) ───────────
# 7 of 11 piles came back as nothing: every answer ran to model.max_tokens and
# stopped mid-string, and a JSON document with no end is not JSON. The
# operations an answer DID finish are good, so they are kept. And of the 18
# operations that arrived, 17 were denied: one memory split into four or five
# operations, each citing every fragment in the pile - which matters, because
# approving an operation archives every short-term it cites.
MAX_OPS_EXTRA = 1            # a pile yields at most its fragments + this many operations
# An operation has to come from the fragments. With the prompt saying so, the
# real 4B still copied an existing CORE's text into an unrelated pile as a new
# verbatim operation (dry run, 30 Sept 2026) - approved, that is a duplicate
# core. So: when both have enough words to judge, at least this share of an
# operation's words must appear somewhere in the pile's fragments.
GROUNDED_MIN = 0.3
GROUNDED_MIN_WORDS = 6
SOURCE_MIN_SHARE = 0.5       # a cited fragment must share at least this much of the best one's words


def salvage_operations(text: str) -> tuple[list[dict[str, Any]], bool]:
    """(the complete operation objects in an answer, whether it was cut off).
    Reads the "operations" array object by object and stops at the first one
    that does not parse - the one the token cap landed in."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start = t.find('"operations"')
    start = t.find("[", start) if start >= 0 else -1
    if start < 0:
        return [], False
    dec = json.JSONDecoder()
    ops: list[dict[str, Any]] = []
    pos = start + 1
    while True:
        while pos < len(t) and t[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(t):
            return ops, True                               # ran out inside the array
        if t[pos] == "]":
            return ops, False
        try:
            obj, pos = dec.raw_decode(t, pos)
        except ValueError:
            return ops, True                               # the object the cap landed in
        if isinstance(obj, dict):
            ops.append(obj)


def cited_fragments(text: str, idxs: list[int], entries: list[dict[str, Any]]) -> list[int]:
    """Of the fragments an operation cites, the ones its text plausibly came
    from: those sharing at least SOURCE_MIN_SHARE of the words the best-matching
    fragment shares. A small model cites every fragment in the pile for every
    operation; approving then archives fragments the operation never covered.
    Under-citing is the safe side - a fragment left out stays in short-term
    for a later sleep. One citation, or no words to compare, is left alone."""
    if len(idxs) < 2:
        return idxs
    words = {w for w in _words(text) if len(w) >= 4 and w not in _HINT_SKIP}
    if not words:
        return idxs
    share = {i: len(words & _words(entries[i].get("content") or "")) for i in idxs}
    best = max(share.values())
    if best == 0:
        return idxs
    return [i for i in idxs if share[i] >= best * SOURCE_MIN_SHARE]


def _significant(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= 4 and w not in _HINT_SKIP}


# Lines of the prompts that have no business inside a memory. On 3 Oct 2026 a
# redraft put "Source fragments (the critique may name them by the id in
# brackets):" and the fragment list INSIDE its content, and it was approved
# on the last attempt with the reviewer editing it out by hand.
SCAFFOLDING = ("Source fragments", "Existing cores:", "THE CRITIQUE", "The critique, once more",
               "What you proposed", "Other operations in this draft", "Previous content:", "Critique:",
               "Steer:", "Fragments:", "Topic:", "JSON:", "Operation kind:", "Target core:")


def strip_scaffolding(text: str) -> tuple[str, bool]:
    """The text up to the first line that is prompt scaffolding, and whether
    anything was cut. A memory is never the prompt echoed back."""
    lines = (text or "").split("\n")
    for i, line in enumerate(lines):
        bare = line.strip()
        if any(bare.startswith(m) for m in SCAFFOLDING):
            return "\n".join(lines[:i]).strip(), True
    return (text or "").strip(), False


COVER_SENTENCE_MIN = 0.3   # a sentence this little of whose words are in the operation was not carried
COVER_SENTENCE_WORDS = 6   # ...when it has at least this many words worth carrying


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", text or "") if x.strip()]


def covered_fragments(text: str, idxs: list[int], entries: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    """Of the fragments an operation cites, the ones it CARRIES. A fragment
    is judged sentence by sentence: one with a sentence of its own that the
    operation says nothing of (fewer than COVER_SENTENCE_MIN of its words
    appear) is cited, not carried. Approving archives every cited fragment;
    on 3 Oct 2026 a two-subject fragment (a backup design, and an unrelated
    second subject) was archived by a core that held only the first, and the
    second was gone. Returns (carried, left): what is left stays in short-term
    for a later sleep, and is said in the log. Paraphrase is fine - a carried
    sentence shares words with the operation even reworded; a subject left
    out shares none."""
    words = _significant(text)
    if not words:
        return list(idxs), []
    carried, left = [], []
    for i in idxs:
        missing = False
        for sent in _sentences(entries[i].get("content") or ""):
            sw = _significant(sent)
            if len(sw) >= COVER_SENTENCE_WORDS and len(sw & words) / len(sw) < COVER_SENTENCE_MIN:
                missing = True
                break
        (left if missing else carried).append(i)
    return carried, left


def grounded(text: str, entries: list[dict[str, Any]]) -> bool:
    """Whether an operation's text plausibly comes from the pile's fragments:
    at least GROUNDED_MIN of its words are in them. True when either side is
    too short to judge - a threshold is for the clear case, not the close one."""
    words = _significant(text)
    pile = set().union(*[_significant(e.get("content") or "") for e in entries]) if entries else set()
    if len(words) < GROUNDED_MIN_WORDS or len(pile) < GROUNDED_MIN_WORDS:
        return True
    return len(words & pile) / len(words) >= GROUNDED_MIN

# Stamped on every draft with the model that wrote it (Memory's /audit groups
# by it). Bump this when a drafting or redrafting prompt changes, so a change
# in the numbers can be told apart from a change of model (26 Sept 2026).
PROMPT_VERSION = "2026-10-03"                             # scaffolding cut from content; an op carries only the fragments it covers


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
        self._fold_quiet_tends()
        self._model_failures: list[dict[str, Any]] = []
        self._withdrawn = 0                                # denied ops the redraft dropped, per tend
        self._hint_hits: dict[tuple[str, str], list[str]] = {}   # per sleep: (promote|noise, hint) -> topics
        self._next_redraft_at = 0.0                        # tend cycle on: the loop redrafts no sooner than this
        self._nudged = False                               # 'your turn': the next tick redrafts whatever the clock says
        self._wake: Optional[Any] = None                   # set by the app: run a tick now instead of at the interval
        self._ripple_retry: Optional[dict[str, Any]] = None   # a ripple that was skipped; fired again on the next tick
        self._cut_off = False                              # the last parsed answer ran out of tokens
        self._cut_offs = 0                                 # how many did, this sleep
        self._served_model = ""          # what the server said it runs, from its last answer
        # While drafting, every model call is kept here and saved as the
        # draft's replay packet (see seren_hippocampus.replay). None = off.
        self._capture: Optional[list[dict[str, Any]]] = None
        self.model = ModelLifecycle(cfg.model, log=self._log, emit=self._emit,
                                    log_dir=self._state_path().parent)
        self.ripple = Ripple(cfg.ripple, log=self._log, transport=notify_transport,
                           log_dir=self._state_path().parent)
        # Whose memories these are, in their words (opt in; seren_hippocampus.voice).
        self.voice = VoiceCard(cfg.voice, self._state_path().parent / "voice.json")

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
        # The ripple: the question, where the webhook is the record. hip-ripple.
        ripple = getattr(self, "ripple", None)          # None while __init__ is still wiring
        if ripple is not None and ripple.wants(kind):
            self._fire_ripple(ev)

    # Events that hand the work to the main model: it is about to load, or to
    # be the one thing running.
    HANDS_OVER = ("draft_submitted", "tend_resubmitted")

    def _fire_ripple(self, ev: dict[str, Any]) -> None:
        if self._cfg.model.lifecycle.handover and ev.get("event") in self.HANDS_OVER:
            # The Nano floor: one model in memory at a time. The draft is out,
            # so the small model goes BEFORE the main one is poked.
            model = getattr(self, "model", None)
            if model is not None:
                try:
                    model.stop_now("handing over to the main model")
                except Exception as e:  # noqa: BLE001
                    self._log(f"hand-over stop failed: {e}")
        ev["ripple"] = self.ripple.fire(ev)
        if (ev["ripple"] or {}).get("skipped"):
            self._ripple_retry = ev                        # see tick()

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
        try:
            text, served = self._call_at(m.url, m.name, prompt, max_tokens or m.max_tokens,
                                         m.extra_body, m.timeout_seconds)
            # llama-server (and most OpenAI-compatible servers) name the model
            # they actually ran - the gguf file, not the config's alias. That
            # is what the audit needs to tell two models apart.
            self._served_model = served or self._served_model
            return text
        finally:
            self.model.release()

    @staticmethod
    def _call_at(url: str, name: str, prompt: str, max_tokens: int, extra_body: Optional[dict],
                 timeout: int) -> tuple[str, str]:
        """One chat call to any OpenAI-compatible server: (answer, the model
        the server says it ran). The drafting path and a replay share it."""
        payload = {"model": name, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens, "temperature": 0.2}
        for k, v in (extra_body or {}).items():
            payload.setdefault(k, v)
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(url.rstrip("/") + "/chat/completions", json=payload)
                r.raise_for_status()
                body = r.json()
                return body["choices"][0]["message"]["content"] or "", str(body.get("model") or "")
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ModelUnavailable(f"the model at {url} stopped answering: {e}") from e
        except httpx.TimeoutException as e:
            raise RuntimeError(f"the model took longer than {timeout}s to answer - a slow model, "
                               f"or one spending its tokens thinking") from e

    def _stamp(self) -> dict[str, Any]:
        """Which model wrote this draft, flat so Memory keeps it as-is: the
        audit's numbers group by it, and a swap is only visible if every draft
        says who wrote it."""
        if not self.model_configured:
            return {"model_mode": "mechanical", "model_prompt": PROMPT_VERSION}
        stamp = {"model_mode": "model", "model_name": self._cfg.model.name,
                 "model_served": self._served_model, "model_url": self._cfg.model.url,
                 "model_prompt": PROMPT_VERSION}
        # Which voice card the prompts carried: a draft written under v3 of
        # someone's card reads differently from one under v1, or none.
        if self.voice.block():
            stamp["model_voice"] = self.voice.version()
        return stamp

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
    def sleep(self, ask_for_brief: bool = False) -> dict[str, Any]:
        """Sleep NOW, by hand (POST /sleep, the viewer's button, the MCP
        sleep_now): on the open brief if there is one. The loop itself never
        sleeps without a brief - see check().

        ask_for_brief (what the button and the tool pass): with no brief
        waiting, do not draft blind - ASK for one, and let the check sleep on
        it when it arrives. The map of the cycle: sleep starts >
        the hippocampus wakes > asks for a brief > the brief is written > the
        draft cycle. A
        sleep started by hand that morning skipped the brief and drafted 20
        operations unsteered; two of them survived first review. Returns
        asked_for_brief: true and records no sleep, so bedtime does not move.
        Where nobody can be asked (brief_request off) it sleeps without one,
        as before.

        ONE CYCLE AT A TIME, by hand too. The tick already waited for an open
        chain to land; a sleep by hand did not, so it could start a second
        draft - and consume a second brief - beside one still under review.
        There should not be multiple drafts, or multiple briefs: a sleep cycle
        completes before another starts. It refuses and says
        why; nothing is recorded as a sleep, so bedtime does not move."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            try:
                if self._open_chain():
                    return {"refused": True, "status": "chain_open", "draft_id": None, "operations": 0,
                             "purged": 0, "error": None, "message": "a sleep cycle is already under way - a draft is waiting for review or a redraft; it finishes (review, redraft, the chain lands and the brief is consumed) before another starts"}
            except MemoryError:
                pass                                       # _sleep records a dead Memory as it always has
            brief = None
            try:
                brief = self._mem.latest_brief()
            except MemoryError:
                pass
            if brief and brief.get("id") in (self.state.get("parked_briefs") or []):
                brief = None                               # set aside: its hints matched nothing
            if brief is None and ask_for_brief and self._cfg.sleep.brief_request:
                try:
                    req = self.request_brief(force=True)
                except MemoryError:
                    req = None                             # Memory is down: _sleep records that as it always has
                if req is not None:
                    self._log("sleep by hand with no brief: asked for one; the check sleeps on it when it arrives")
                    return {"asked_for_brief": True, "status": "asked_for_brief", "draft_id": None,
                            "operations": 0, "purged": 0, "error": None,
                            "intent_id": req.get("intent_id"),
                            "message": "no brief is waiting, so the hippocampus asked for one instead of drafting "
                                       "blind; it sleeps on the brief at the next check after it arrives"}
            return self._sleep(brief=brief)
        finally:
            self._lock.release()

    def tend(self, redraft: bool = True) -> dict[str, Any]:
        """Tend the open chains. By hand (tend_now, POST /tend) it always
        redrafts. The loop passes redraft=False when the tend cycle says not
        now: chains that landed are still closed, denied operations wait."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._tend(redraft)
        finally:
            self._lock.release()

    def redraft_due(self, now: Optional[float] = None) -> bool:
        """Whether this turn of the loop may redraft - the one part of a tend
        that starts the small model. Tend cycle on: every tend_interval_seconds.
        Off: only at sleep time, which is bedtime having passed or a NEW brief
        waiting (not the open chain's own). Bedtime has to count: an open
        chain keeps its brief open, so the check never asks for another one,
        and a redraft waiting only for a new brief would wait for ever."""
        now = time.time() if now is None else now
        s = self._cfg.sleep
        if self._nudged:
            # The reviewer said 'your turn': the verdicts are in and it has
            # left. No timer and no tend-cycle setting stands between that and
            # the redraft - which is what lets a box with the tend cycle off
            # still finish a chain in one go.
            self._nudged = False
            self._next_redraft_at = now + s.tend_interval_seconds
            return True
        if s.tend_cycle:
            if now < self._next_redraft_at:
                return False
            self._next_redraft_at = now + s.tend_interval_seconds
            return True
        if now >= self.next_sleep_due(now):
            return True
        return self._new_brief_waiting()

    def _new_brief_waiting(self) -> bool:
        """An open brief that no draft still in play was written from: the
        main model saying a sleep may start."""
        try:
            brief = self._mem.latest_brief()
            if not brief or brief.get("id") in (self.state.get("parked_briefs") or []):
                return False
            used = {d.get("brief_id_used") for st in ("pending", "reviewed") for d in self._mem.drafts(status=st)}
            return brief.get("id") not in used
        except MemoryError:
            return False

    def check(self, now: Optional[float] = None) -> dict[str, Any]:
        """Check for a brief; sleep on it if one is there and no chain is
        open; count the misses after bedtime and ask when they add up."""
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._check(now)
        finally:
            self._lock.release()

    def nudge(self) -> dict[str, Any]:
        """'Your turn.' The main model's last act after writing a brief or
        reviewing a draft: the hippocampus carries on NOW instead of at the
        next tick - sleeps on the brief, redrafts what was denied, closes
        what landed. Neuron to neuron: the hippocampus
        could wake the model, and the model could only wait for a timer. On
        1 Oct a chain with five minutes of work in it took twenty-five.

        Returns at once; the work happens on the loop. On a cluster the same
        call arrives from the Observatory once it has the brief and has
        stopped the main model's server."""
        self._nudged = True
        woke = False
        if self._wake is not None:
            try:
                self._wake()
                woke = True
            except Exception as e:  # noqa: BLE001 - the next tick still honours the nudge
                self._log(f"nudge could not wake the loop: {e}")
        self._emit("nudged", woke=woke)
        return {"ok": True, "woke": woke,
                "message": "carrying on now" if woke else "noted; the next tick carries on"}

    def tick(self) -> dict[str, Any]:
        """One turn of the loop: tend the open chains, check for a brief, and
        stop a model this hippocampus started once it is not needed."""
        if self._ripple_retry is not None:
            # A ripple that could not start (the last one for that event was
            # still running): nobody was told. Tell them now.
            ev, self._ripple_retry = self._ripple_retry, None
            self._fire_ripple(ev)
        out = {"tend": self.tend(redraft=self.redraft_due()), "check": self.check()}
        try:
            # The cycle is over when no chain is open: nothing will call the
            # model again before the next sleep, so it goes now rather than
            # sitting loaded for keep_warm_seconds. Mid-chain it stays warm
            # for the redraft (unless handover already stopped it).
            if self.model.started_by_us and not self._open_chain():
                out["model_stopped"] = self.model.stop_now("the cycle is over")
            else:
                out["model_stopped"] = self.model.maybe_stop()
        except Exception as e:  # noqa: BLE001
            self._log(f"model stop check failed: {e}")
        return out

    # ── the check: is there a brief? ──────────────────────────────────────
    def _open_chain(self) -> bool:
        """A draft is still in play: pending verdicts, or denied operations
        that tend will redraft. One chain at a time - a brief that arrives
        while one is open waits its turn."""
        if self._mem.drafts(status="pending"):
            return True
        for d in self._mem.drafts(status="reviewed"):
            denied = [op for op in (d.get("operations") or []) if op.get("status") == "denied"]
            if not denied or d.get("terminal"):
                continue                                   # tend will close it
            chain = self._mem.draft_chain(d["id"])
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
            flags = self._mem.tidy(age_out=False, near=False, sweep=False, purge=True)
            purged = flags.get("purged") or []
            if purged:
                self._log(f"purged {len(purged)} flagged memories")
                self._emit("purged", count=len(purged), ids=[t.get("id") for t in purged])
            restored = flags.get("restored") or []
            if restored:
                # Cores flagged to get their earlier wording back (Memory's
                # undo-restate): the same gate as forget - asked for with a
                # reason, carried out here.
                self._log(f"put back the earlier wording of {len(restored)} core(s): " +
                          ", ".join(str(t.get("id")) + (f" ({t['error']})" if t.get("error") else "") for t in restored))
                self._emit("restored", count=len(restored), ids=[t.get("id") for t in restored])

            brief = self._mem.latest_brief()
            if brief and brief.get("id") in (self.state.get("parked_briefs") or []):
                # Its hints matched nothing and it drafted nothing: it waits for
                # a rewrite (a newer brief) instead of sleeping on every tick.
                report["brief_id"] = brief.get("id")
                report["status"] = "brief_unmatched"
            elif brief:
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

    def request_brief(self, now: Optional[float] = None, force: bool = False) -> Optional[dict[str, Any]]:
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
        if not force and self._mem.latest_brief():
            return None                                   # the model got there first
        when = time.strftime("%H:%M", time.localtime(due)) if due > now else "soon"
        text = (f"The hippocampus wants a brief before it sleeps at {when}: what mattered since the last "
                "sleep, promote_hints for the running bits and the real lessons, noise_hints for the one-offs. "
                "submit_brief on Memory writes it; without one the small model pulls a guess from the fragments.")
        if force:
            # Asked for by hand (sleep(ask_for_brief=True)): the sleep is wanted
            # now, whatever the clock says, and it waits for the brief.
            due, when = now, "now"
            text = ("A sleep was started by hand and the hippocampus wants a brief before it drafts: what "
                    "mattered since the last sleep, promote_hints for the running bits and the real lessons, "
                    "noise_hints for the one-offs. submit_brief on Memory writes it; the sleep starts at the "
                    "next check after it arrives.")
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
                # Retired, not completed: Memory turns a completed intent into a
                # long-term 'Completed intent: ...' record at the next tidy, and
                # a poke from this service is not a memory. See _retire_note.
                self._mem.delete_near(str(req["intent_id"]))
            except Exception as e:  # noqa: BLE001
                self._log(f"could not retire the brief request: {e}")

    # ── sleep ─────────────────────────────────────────────────────────────
    def _sleep(self, brief: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        start = time.time()
        catch_up = self.is_catch_up(start)
        report: dict[str, Any] = {"started_at": start, "purged": 0, "brief_id": None,
                                  "brief_pulled": False, "clusters": 0, "operations": 0,
                                  "draft_id": None, "held_back": 0, "tidy": {}, "error": None,
                                  "catch_up": catch_up, "quiet": False,
                                  "brief_requested": self.brief_wanted() is not None, "brief_answered": None}
        self._model_failures = []
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
                # Remember when the brief used was written: at the cull it is consumed
                # and gone from the open list, and older open briefs retire with it.
                times = dict(self.state.get("brief_times") or {})
                times[str(brief.get("id"))] = float((brief.get("metadata") or {}).get("created_at", 0) or 0)
                self.state["brief_times"] = dict(list(times.items())[-50:])
                report["brief_pulled"] = bool(brief.get("_pulled"))
                meta = brief.get("metadata") or {}
                promote_hints = {str(h).lower() for h in (meta.get("promote_hints") or [])}
                noise_hints = {str(h).lower() for h in (meta.get("noise_hints") or [])}

            # 3. the draft
            shorts = self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)
            held = self._held_short_ids()
            free = [s for s in shorts if s["id"] not in held]
            report["held_back"] = len(shorts) - len(free)
            clusters = cluster_by_tags(free)
            report["clusters"] = len(clusters)

            ops: list[dict[str, Any]] = []
            self._capture = []
            self._hint_hits = {}
            self._cut_offs = 0
            for topic, entries in clusters:
                ops += self._shifted(self._propose(topic, entries, promote_hints, noise_hints, brief), len(ops))
            ops = self._submittable(ops)
            report["operations"] = len(ops)
            if self._cut_offs:
                # Answers that ran out of tokens: their finished operations were
                # kept, the rest was lost. The number that says 'raise max_tokens'.
                report["cut_off"] = self._cut_offs

            # What each hint matched, said out loud. A brief whose promote hints
            # matched nothing used to be consumed as 'ok, 0 operations' - the
            # same as a night where nothing happened (hip-brief-unmatched).
            if brief and not brief.get("_pulled") and (promote_hints or noise_hints):
                report["hints"] = {
                    "promote": {h: self._hint_hits.get(("promote", h), []) for h in sorted(promote_hints)},
                    "noise": {h: self._hint_hits.get(("noise", h), []) for h in sorted(noise_hints)}}
                if promote_hints and not report["quiet"] and not any(report["hints"]["promote"].values()):
                    report["brief_unmatched"] = True
                    self._log(f"brief {brief.get('id')}: none of its promote hints matched a memory "
                              f"({', '.join(sorted(promote_hints))})")
                    self._emit("brief_unmatched", brief_id=brief.get("id"), hints=sorted(promote_hints))
            if ops:
                summary = self._summarise(ops)
                out = self._mem.submit_draft({"summary": summary, "operations": ops,
                                               "brief_id_used": report["brief_id"], "attempt": 1,
                                               # max_attempts 1 = this is the only attempt
                                               "terminal": self._cfg.sleep.max_attempts <= 1,
                                               "extra": self._stamp()})
                report["draft_id"] = out.get("id")
                self._save_replay(out.get("id"), attempt=1, brief_id=report["brief_id"])
                self._log(f"submitted draft {out.get('id')} with {len(ops)} operation(s)")
                # Poke the model: a note in Memory that names the draft, closed
                # when the chain lands. The event goes out for the webhook.
                note_id = None
                try:
                    note = self._mem.add_near(
                        f"A draft with {len(ops)} operation(s) waits for your review: review_draft "
                        f"{out.get('id')}. Approve or deny each operation; a denial needs a critique.",
                        topic="hippocampus, review", trigger_type="always")
                    note_id = note.get("id")
                    notes = dict(self.state.get("review_notes") or {})
                    notes[str(out.get("cluster_id") or out.get("id"))] = note_id
                    self.state["review_notes"] = notes
                except Exception as e:  # noqa: BLE001 - the note is a courtesy, the draft is the record
                    self._log(f"could not leave the review note: {e}")
                # The tidy waits for the chain to land; remember whether this
                # was a catch-up so the tidy then keeps its promise.
                pending = dict(self.state.get("chain_catch_up") or {})
                pending[str(out.get("cluster_id") or out.get("id"))] = catch_up
                self.state["chain_catch_up"] = pending
                self._emit("draft_submitted", draft_id=out.get("id"), operations=len(ops),
                           kinds=sorted({op["kind"] for op in ops}), review_note_id=note_id)
            elif self._model_failures:
                # Nothing proposed BECAUSE the model failed: the brief is kept
                # for the next check, and the sleep says why it stopped.
                raise RuntimeError("the model could not draft: " + "; ".join(
                    f"{f['topic'] or 'untagged'}: {f['why']}" for f in self._model_failures[:3]))
            elif brief and not brief.get("_pulled") and brief.get("id") and report.get("brief_unmatched"):
                # Nothing drafted BECAUSE the hints matched nothing: the brief is
                # parked, not consumed, so the model can rewrite it (a new brief
                # supersedes it). The check skips a parked brief, or it would
                # sleep on it again every tick.
                parked = list(self.state.get("parked_briefs") or [])
                if brief["id"] not in parked:
                    self.state["parked_briefs"] = (parked + [brief["id"]])[-50:]
            elif brief and not brief.get("_pulled") and brief.get("id"):
                # A brief with nothing to draft from: nothing to review, so the
                # brief has done its job. Consume it or the check loops forever.
                try:
                    self._mem.consume_brief(str(brief["id"]))
                    self._emit("brief_consumed", brief_id=brief["id"], draft_id=None)
                except Exception as e:  # noqa: BLE001
                    self._log(f"could not consume the brief: {e}")

            # 4. tidy - the end of the cycle. Aging out is
            # always at the end of a sleep, and a sleep is the whole cycle -
            # draft, review, redraft, the chain landing. With a draft out the
            # cycle is not over, so the tidy waits for _close_chain; age-out
            # then never runs under a draft (a short-term whose operation was
            # denied is not 'held', and could otherwise age out before its
            # redraft). A catch-up keeps everything that has not been looked at.
            if report["draft_id"]:
                report["tidy"] = {"deferred": "until the chain lands"}
            else:
                report["tidy"] = self._mem.tidy(age_out=not catch_up, near=True, sweep=not catch_up, purge=False)
        except (MemoryError, Exception) as e:  # noqa: BLE001 - a sleep records its failure and ends
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"sleep error: {report['error']}")
            self._emit("sleep_failed", error=report["error"])
        report["model_failures"] = list(self._model_failures)
        report["finished_at"] = time.time()
        report["duration_seconds"] = round(report["finished_at"] - start, 2)
        self.state["last_sleep"] = report
        self._remember_run("sleep", report, operations=report["operations"], purged=report["purged"],
                           draft_id=report["draft_id"], quiet=report["quiet"], catch_up=report["catch_up"])
        if not report["error"]:
            self._emit("sleep_done", operations=report["operations"], purged=report["purged"],
                       quiet=report["quiet"], catch_up=report["catch_up"],
                       brief_answered=report["brief_answered"])
        self._save_state()
        return report

    def _close_chain(self, d: dict[str, Any], chain: list[dict[str, Any]]) -> dict[str, Any]:
        """The cull. After the approved operations have been written to long
        by the review, close every reviewed draft in the chain, consume the
        brief that opened it, complete the review note, say so - and end the
        sleep cycle with its tidy (age out, near-term, sweep). Returns the
        tidy's report."""
        cluster_id = d.get("cluster_id") or d["id"]
        for x in chain or [d]:
            if x.get("status") == "reviewed":
                try:
                    self._mem.close_draft(x["id"])
                except Exception as e:  # noqa: BLE001
                    self._log(f"could not close draft {x['id']}: {e}")
        brief_id = None
        for x in (chain or [d]):
            if x.get("brief_id_used"):
                brief_id = x["brief_id_used"]
                break
        if brief_id:
            try:
                self._mem.consume_brief(str(brief_id), draft_id=d["id"])
                self._emit("brief_consumed", brief_id=brief_id, draft_id=d["id"])
            except Exception as e:  # noqa: BLE001
                self._log(f"could not consume brief {brief_id}: {e}")
            self._retire_older_briefs(str(brief_id), d["id"])
        notes = dict(self.state.get("review_notes") or {})
        note_id = notes.pop(str(cluster_id), None)
        if note_id:
            try:
                # The review note is this service's poke, not something the
                # person meant to do: completing it made Memory's tidy file
                # 'Completed intent: A draft with N operation(s) waits for your
                # review...' in long-term, one per chain (seen live 27 Sept
                # 2026). Deleting an open loop is the sanctioned way to drop one.
                self._mem.delete_near(str(note_id))
            except Exception as e:  # noqa: BLE001
                self._log(f"could not retire the review note: {e}")
        self.state["review_notes"] = notes
        self.state["brief_misses"] = 0
        pending = dict(self.state.get("chain_catch_up") or {})
        catch_up = bool(pending.pop(str(cluster_id), False))
        self.state["chain_catch_up"] = pending
        try:
            tidy = self._mem.tidy(age_out=not catch_up, near=True, sweep=not catch_up, purge=False)
        except Exception as e:  # noqa: BLE001 - the chain is closed; the next cycle's end tidies
            tidy = {"error": f"{type(e).__name__}: {e}"}
            self._log(f"could not tidy at the end of chain {cluster_id}: {e}")
        self._emit("chain_closed", cluster_id=cluster_id, draft_id=d["id"], brief_id=brief_id,
                   attempts=len(chain or [d]), aged_out=tidy.get("aged_out"))
        self._log(f"chain {cluster_id} closed after {len(chain or [d])} attempt(s)")
        return tidy

    def _retire_older_briefs(self, used_id: str, draft_id: str) -> None:
        """The sleep ran on the newest open brief. Any brief still open that
        was written BEFORE it covers the same period and was superseded by it;
        left open, it would open a second sleep under an outdated steer (seen
        live 25 Sept 2026: two briefs from one night, only the newer consumed).
        A brief written AFTER the one used is left alone - it is the next
        sleep's."""
        cut = float((self.state.get("brief_times") or {}).get(used_id) or 0)
        if not cut:
            return
        try:
            for b in self._mem.open_briefs():
                t = float((b.get("metadata") or {}).get("created_at", 0) or 0)
                if b.get("id") != used_id and t and t < cut:
                    self._mem.consume_brief(str(b["id"]), draft_id=f"superseded-by-{used_id}")
                    self._emit("brief_consumed", brief_id=b["id"], draft_id=None, superseded_by=used_id)
        except Exception as e:  # noqa: BLE001
            self._log(f"could not retire older briefs: {e}")

    def _model_failure(self, stage: str, topic: Any, why: str) -> None:
        """A draft or redraft the model could not produce. Collected on the
        run's report and emitted, so a timeout or an unusable answer is never
        mistaken for 'nothing to propose'."""
        rec = {"stage": stage, "topic": topic, "why": why[:300]}
        self._model_failures.append(rec)
        self._log(f"{stage} failed for {topic or 'untagged'}: {why[:200]}")
        self._emit(f"{stage}_failed", topic=topic, why=why[:300])

    def _fold_quiet_tends(self) -> None:
        """A state file from before quiet tends stopped being listed is full
        of them. Fold them into the count once, at start, so the history
        shows what happened and not forty rows of nothing."""
        rows = list(self.state.get("history") or [])
        quiet = [r for r in rows if r.get("kind") == "tend" and not r.get("error")
                 and not any(r.get(k) for k in ("resubmitted", "ended", "closed", "deferred"))]
        if not quiet:
            return
        q = dict(self.state.get("quiet_tends") or {})
        q["count"] = int(q.get("count") or 0) + len(quiet)
        q.setdefault("since", min((r.get("started_at") or 0) for r in quiet))
        q["last_at"] = max(q.get("last_at") or 0, max((r.get("finished_at") or 0) for r in quiet))
        self.state["quiet_tends"] = q
        self.state["history"] = [r for r in rows if r not in quiet]

    def _remember_run(self, kind: str, report: dict[str, Any], **summary: Any) -> None:
        """A bounded history for the viewer: what each run did or why it stopped.

        A TEND THAT FOUND NOTHING TO DO IS COUNTED, NOT LISTED. The loop tends
        every few minutes, and nearly every one of those closes nothing and
        redrafts nothing; listed, they filled the history in about three hours
        and pushed last night's sleep out of it (with every tend
        shown, reaching the events was a chore). Sleeps and tends
        are each kept to their own last 40, so neither crowds the other out."""
        if (kind == "tend" and not report.get("error") and not report.get("model_failures")
                and not any(summary.get(k) for k in ("resubmitted", "ended", "closed", "deferred"))):
            q = dict(self.state.get("quiet_tends") or {})
            q["count"] = int(q.get("count") or 0) + 1
            q.setdefault("since", report.get("started_at"))
            q["last_at"] = report.get("finished_at")
            self.state["quiet_tends"] = q
            return
        rows = list(self.state.get("history") or [])
        rows.append({"kind": kind, "started_at": report.get("started_at"),
                     "finished_at": report.get("finished_at"),
                     "duration_seconds": round((report.get("finished_at") or 0) - (report.get("started_at") or 0), 2),
                     "error": report.get("error"), **summary})
        keep: dict[str, int] = {}
        kept = []
        for r in reversed(rows):
            keep[r.get("kind")] = keep.get(r.get("kind"), 0) + 1
            if keep[r.get("kind")] <= 40:
                kept.append(r)
        self.state["history"] = list(reversed(kept))

    def _held_short_ids(self) -> set[str]:
        """Short-terms already spoken for by a pending draft. Proposing them
        again every sleep is how the old consolidator made duplicate drafts."""
        held: set[str] = set()
        for d in self._mem.drafts(status="pending"):
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
        tags = _tags(topic).union(*[_tags((e.get("metadata") or {}).get("topic")) for e in entries])
        hay_words = _words(haystack)
        kept = sorted(h for h in promote_hints if hint_match(h, tags, haystack, hay_words))
        noise = sorted(h for h in noise_hints if hint_match(h, tags, haystack, hay_words, loose=False))
        hits = getattr(self, "_hint_hits", None)
        if hits is not None:
            for h in kept:
                hits.setdefault(("promote", h), []).append(topic)
            for h in noise:
                hits.setdefault(("noise", h), []).append(topic)
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
        # A configured model that failed is recorded (see _model_failure) and
        # proposes nothing for this cluster - its short-terms stay free for the
        # next sleep. It used to fall back to copying the longest fragment
        # verbatim and call that a draft (seen on the first real sleep, 25 Sept 2026).
        # A model that answered and proposed nothing is taken at its word.
        ops += self._shifted(drafted, len(ops))
        return ops

    MAX_CANDIDATE_QUERIES = 8

    def _candidates(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The existing cores to show the drafting model for this pile.

        ONE SEARCH PER FRAGMENT, then merged by rank. One search on every
        fragment glued together and cut at 1,500 characters found cores near
        the pile's average, which is near nothing in particular.

        A SATELLITE HIT COUNTS FOR ITS CORE. Memory's search fetched twice the
        rows asked for and then threw the satellites away, so a pile about
        something already well recorded - whose nearest rows are that core's
        own episodes - came back with the core crowded out (2 Oct 2026: 9 of
        30 first-round denials were 'wrong core'). The satellite says which
        core it belongs to; that core is the candidate, and the satellite is
        shown under it as a sample of what already hangs there."""
        want = max(1, int(self._cfg.sleep.candidate_cores))
        texts = [(e.get("content") or "").strip() for e in entries]
        queries = [t[:1500] for t in texts if t][: self.MAX_CANDIDATE_QUERIES]
        if len(queries) > 1:
            queries.append(" ".join(texts)[:1500])
        score: dict[str, float] = {}
        row: dict[str, dict[str, Any]] = {}
        via: dict[str, str] = {}
        failed = None
        for q in queries:
            try:
                hits = self._mem.search_long(q, n=want)
            except MemoryError as e:
                failed = e
                continue
            for rank, h in enumerate(hits):
                meta = h.get("metadata") or {}
                if meta.get("superseded_by"):
                    continue
                if meta.get("kind", "core") == "satellite":
                    cid = meta.get("core_id")
                    if not cid:
                        continue
                    via.setdefault(cid, h.get("content") or "")
                else:
                    cid = h["id"]
                    row[cid] = h
                score[cid] = score.get(cid, 0.0) + 1.0 / (60 + rank)
        if failed is not None and not score:
            self._log(f"candidate search failed ({failed}); proposing without existing cores")
            return []
        out = []
        for cid in sorted(score, key=lambda c: -score[c])[:want]:
            h = row.get(cid)
            if h is None:                                   # reached only through a satellite
                h = self._mem.core(cid)
                if h is None or (h.get("metadata") or {}).get("superseded_by"):
                    continue
            meta = h.get("metadata") or {}
            sats = h.get("satellites")
            if sats is None:
                sats = (h.get("surroundings") or {}).get("satellites")
            c = {"id": cid, "content": h.get("content") or "", "topic": meta.get("topic")}
            if sats:
                c["satellites"] = int(sats)
            if via.get(cid):
                c["via"] = via[cid]
            out.append(c)
        return out

    def _core_lines(self, candidates: list[dict[str, Any]]) -> str:
        """The cores as the model reads them: the id, the topic, enough of the
        text to tell what the core already says, how many episodes hang under
        it, and one of them when that is how it was found."""
        n = max(100, int(self._cfg.sleep.core_chars))
        lines = []
        for c in candidates:
            text = c.get("content") or ""
            line = f"({c['id']})" + (f" [{c['topic']}]" if c.get("topic") else "") + \
                   f" {text[:n]}" + ("..." if len(text) > n else "")
            if c.get("satellites"):
                line += f"  ({c['satellites']} episode(s) already attached)"
            if c.get("via"):
                line += f"\n      one of its episodes: {c['via'][:200]}"
            lines.append(line)
        return "\n".join(lines) or "(none)"

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
        core_lines = self._core_lines(candidates)
        steer_block = f"\n\nSteer:\n{steer}" if steer else ""
        prompt = (
            f"{self.voice.block()}"
            "You are a memory consolidator's drafting worker. Below are short-term memory fragments "
            "about one topic, and the existing long-term CORES closest to them. Propose operations on "
            "long-term memory. Return ONLY JSON in this shape:\n"
            '{"operations": [{"kind": "new_core|attach|supersede", "content": "...", '
            '"target_core_id": "...", "restated_content": "...", "rationale": "...", "source_indexes": [0, 1]}]}\n'
            "Rules: attach = the fragments are more evidence for an existing core (content = a one-line "
            "episode summary; restated_content only if the core's wording should change). supersede = the "
            "fragments contradict or replace an existing core (content = the new statement). new_core = "
            "nothing existing covers it (content = one durable statement, present tense, no preamble). "
            "Use core ids exactly as given and never "
            "invent one. To attach to (or supersede) a new_core you propose in this same answer, give "
            '"target_op": <that new_core\'s position in your list, from 0> instead of target_core_id. '
            "source_indexes lists ONLY the fragments that operation's text actually comes from, not "
            "every fragment shown: a fragment is archived when its operation is approved, so citing one "
            "the operation does not cover loses it. One operation for each separate memory the "
            "fragments hold, never more operations than fragments, and none for a fragment an existing "
            "core already covers: a memory is one operation, not one per sentence. Say only what the "
            "fragments say - nothing from the cores or the card that the fragments do not. Leave restated_content out unless "
            "the core's own wording must change, and never repeat content in it. Keep each rationale to "
            "one short sentence. Weigh the steer when there is one: it says what mattered, which the "
            "fragments alone cannot."
            f"{steer_block}\n\n"
            f"Topic: {topic or 'untagged'}\n\nFragments:\n{frag_lines}\n\nExisting cores:\n{core_lines}\n\nJSON:")
        call: dict[str, Any] = {"stage": "draft", "topic": topic, "prompt": prompt,
                                "entries": [{"id": e["id"], "content": e.get("content") or ""} for e in entries],
                                "candidates": candidates}
        if self._capture is not None:
            self._capture.append(call)
        t0 = time.time()
        try:
            raw = self._call_model(prompt)
        except ModelUnavailable:
            raise                                          # the sleep fails and keeps its brief
        except Exception as e:  # noqa: BLE001
            call.update(error=str(e), seconds=round(time.time() - t0, 2), ops=[])
            self._model_failure("draft", topic, str(e))
            return []
        call.update(answer=raw, seconds=round(time.time() - t0, 2))
        ops = self._parse_draft(raw, topic, entries, candidates)
        call["ops"] = ops or []
        cut = self._cut_off
        cap = self._cfg.model.max_tokens
        if cut:
            call["cut_off"] = True
            self._cut_offs += 1
        if ops is None:
            # Say WHICH failure: 'not the JSON asked for' sent the first reader
            # looking for a formatting bug when the answer had simply run out
            # of tokens (seven piles on 30 Sept 2026).
            call["error"] = "cut off at max_tokens" if cut else "not the JSON asked for"
            self._model_failure("draft", topic, (
                f"the answer was cut off before one operation was complete: model.max_tokens ({cap}) "
                f"is too low for this model" if cut else
                "the model's answer was not the JSON asked for: " + repr((raw or "")[:160])))
            return []
        if cut:
            self._log(f"{topic or 'untagged'}: the answer was cut off at model.max_tokens ({cap}); "
                      f"kept its {len(ops)} complete operation(s)")
        return ops

    def _parse_draft(self, raw: str, topic: Optional[str], entries: list[dict[str, Any]],
                     candidates: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
        """A drafting answer, parsed and validated: None when it is not the
        JSON asked for. The sleep and a replay both come through here. An
        answer cut off at the token cap keeps the operations it finished
        (self._cut_off says it happened)."""
        self._cut_off = False
        parsed = self._json_from(raw)
        if not isinstance(parsed, dict):
            ops, self._cut_off = salvage_operations(raw)
            if not ops:
                return None
            return self._validate_ops(ops, topic, entries, candidates)
        return self._validate_ops(parsed.get("operations"), topic, entries, candidates)

    def _validate_ops(self, raw_ops: Any, topic: Optional[str], entries: list[dict[str, Any]],
                      candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only what the store will accept: known kinds, real core ids, real
        fragment indexes, non-empty content. A model that invents an id gets
        that operation dropped, not the whole draft.

        target_op (an attach / supersede on a new_core in the same answer) is
        the model's position in ITS list; ops dropped here shift positions, so
        it is remapped to the returned list, and an op whose new_core did not
        survive is dropped like an invented target."""
        core_ids = {c["id"] for c in candidates}
        kept: list[tuple[int, dict[str, Any], Optional[int]]] = []   # (answer position, op, its target_op)
        for pos, raw in enumerate(raw_ops or []):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "")).strip().lower()
            if kind not in OP_KINDS:
                continue
            if kind == "verbatim":
                # WORD FOR WORD IS THE REVIEWER'S CALL, NOT THE WORKER'S. A
                # verbatim core exists because the main model marked a memory
                # (preserve_memory_verbatim): 'I want this word for word, like
                # a promise, an important moment'. Those
                # are made in _propose with no model at all. A worker that
                # picks the kind itself writes a blend and calls it exact
                # (seen live the same day); what it wrote is a new core.
                kind = "new_core"
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
            content, cut = strip_scaffolding(str(raw.get("content") or ""))
            if cut:
                self._log(f"{topic or 'untagged'}: cut prompt scaffolding out of a {kind}'s text")
            target = raw.get("target_core_id")
            target_op: Optional[int] = None
            if kind in ("attach", "supersede"):
                if target not in core_ids:
                    try:
                        target_op = int(raw.get("target_op"))
                    except (TypeError, ValueError):
                        continue
                    target = None
            else:
                target = None
            if kind != "attach" and not content:
                continue
            restated = str(raw.get("restated_content") or "").strip() if kind == "attach" else ""
            # The episode summary repeated as the core's new wording: approving
            # it would REPLACE the core's text with the episode. It is not a
            # restatement, so it goes.
            if restated == content:
                restated = ""
            if not grounded(f"{content} {restated}", entries):
                self._log(f"{topic or 'untagged'}: dropped a {kind} whose text is not in the fragments "
                          f"(copied from a core or the card?): {content[:90]!r}")
                continue
            if kind != "verbatim":
                # what the text covers first, then which of those it came from:
                # a two-subject fragment shares the most words of all and would
                # otherwise crowd the fragments the operation does carry out
                idxs, left = covered_fragments(f"{content} {restated}", sorted(set(idxs)), entries)
                idxs = cited_fragments(f"{content} {restated}", idxs, entries)
                if left:
                    self._log(f"{topic or 'untagged'}: a {kind} cites {len(left)} fragment(s) it carries less than "
                              f"half of; they stay in short-term: " + ", ".join(entries[i]["id"][:8] for i in left))
            op = {"kind": kind, "content": content, "topic": topic, "target_core_id": target,
                  "source_short_ids": [entries[i]["id"] for i in sorted(set(idxs))],
                  "evidence_count": len(set(idxs)),
                  "rationale": str(raw.get("rationale") or "")[:500] or None}
            if restated:
                op["restated_content"] = restated
            kept.append((pos, op, target_op))
        # new_cores that survived, by answer position; an op on one that did
        # not (or on itself, or on anything but a new_core) goes
        cores_at = {pos for pos, op, _ in kept if op["kind"] == "new_core"}
        kept = [(pos, op, t) for pos, op, t in kept if t is None or (t in cores_at and t != pos)]
        # One memory is one operation. A pile that came back with more than its
        # fragments (plus one) is cut to the first ones. No number is named in
        # the prompt: 'at most three' made a 4B write exactly three, every
        # time (dry run on the real model, 30 Sept 2026). AFTER the invalid
        # ones are gone, so junk does not use up the room; an op on a new_core
        # that was cut then goes with it.
        cap = len(entries) + MAX_OPS_EXTRA
        if len(kept) > cap:
            self._log(f"{topic or 'untagged'}: {len(kept)} operations from {len(entries)} fragment(s); "
                      f"keeping the first {cap}")
            kept = kept[:cap]
            cores_at = {pos for pos, op, _ in kept if op["kind"] == "new_core"}
            kept = [(pos, op, t) for pos, op, t in kept if t is None or t in cores_at]
        final = {pos: i for i, (pos, _, _) in enumerate(kept)}
        out: list[dict[str, Any]] = []
        for _, op, t in kept:
            if t is not None:
                op["target_op"] = final[t]
            out.append(op)
        return out

    def _submittable(self, ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """ops with target_op go only to a Memory that says it takes them
        (features on /health). An older Memory drops the unknown field and
        then refuses the WHOLE draft - an attach with no target_core_id - so
        there the dependents are left out and their short-terms wait for a
        later sleep. Nothing else moves: only new_cores are ever targeted, and
        no op that stays points anywhere."""
        if not any(op.get("target_op") is not None for op in ops):
            return ops
        try:
            takes = "target_op" in (self._mem.health().get("features") or [])
        except Exception:  # noqa: BLE001 - unknown means no
            takes = False
        if takes:
            return ops
        kept = [op for op in ops if op.get("target_op") is None]
        self._log(f"memory does not take target_op (older build): {len(ops) - len(kept)} operation(s) "
                  "on a new core in this draft left for a later sleep")
        return kept

    @staticmethod
    def _shifted(ops: list[dict[str, Any]], base: int) -> list[dict[str, Any]]:
        """ops about to be appended after `base` others: their target_op
        positions move with them."""
        if base:
            for op in ops:
                if op.get("target_op") is not None:
                    op["target_op"] += base
        return ops

    @staticmethod
    def _summarise(ops: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for op in ops:
            counts[op["kind"]] = counts.get(op["kind"], 0) + 1
        parts = [f"{n} {k}" for k, n in counts.items()]
        topics = sorted({op.get("topic") or "untagged" for op in ops})
        return f"{', '.join(parts)} across {', '.join(topics)[:200]}"

    # ── tend ──────────────────────────────────────────────────────────────
    def _tend(self, redraft: bool = True) -> dict[str, Any]:
        start = time.time()
        self._model_failures = []
        report: dict[str, Any] = {"started_at": start, "examined": 0, "resubmitted": [],
                                  "ended": [], "closed": [], "deferred": [], "tidy": {}, "error": None}
        try:
            reviewed = self._mem.drafts(status="reviewed")
            report["examined"] = len(reviewed)
            for d in reviewed:
                denied = [op for op in (d.get("operations") or []) if op.get("status") == "denied"]
                cluster_id = d.get("cluster_id") or d["id"]
                chain = self._mem.draft_chain(d["id"])
                if any(int(x.get("attempt", 1)) > int(d.get("attempt", 1)) for x in chain):
                    continue                                   # already resubmitted
                if not denied or d.get("terminal"):
                    # The chain has landed (every verdict in, approvals applied) or
                    # is spent (the last attempt denied): cull.
                    if denied:
                        report["ended"].append(cluster_id)     # shorts stay for a later sleep
                        if cluster_id not in (self.state.get("ended_chains") or []):
                            self.state["ended_chains"] = (self.state.get("ended_chains") or [])[-200:] + [cluster_id]
                            self._emit("chain_ended", cluster_id=cluster_id, draft_id=d["id"])
                    report["tidy"][cluster_id] = self._close_chain(d, chain)
                    report["closed"].append(cluster_id)
                    continue
                if not redraft:
                    # Not now (see redraft_due): the chain stays open and its
                    # denied operations wait. Nothing above this line needed
                    # the model; everything below does.
                    report["deferred"].append(cluster_id)
                    continue
                self._capture = []
                failures = len(self._model_failures)
                self._withdrawn = 0
                new_ops = self._redraft(self._resolve_target_ops(denied, d), made=self._chain_cores(chain),
                                        draft_ops=d.get("operations") or [])
                if self._withdrawn:
                    report.setdefault("withdrawn", {})[cluster_id] = self._withdrawn
                if not new_ops:
                    self._capture = None
                    if len(self._model_failures) > failures:
                        continue                               # the model failed: the next tend tries again
                    # Nothing to resubmit and nothing failed: every denied op was
                    # withdrawn, or none could be redrafted. Either way the chain
                    # can never move again; left open it would hold every later
                    # sleep off (one cycle at a time) and its age-out forever. End it.
                    why = "withdrawn" if self._withdrawn == len(denied) else "nothing to redraft"
                    self._log(f"chain {cluster_id}: {why}; ending the chain")
                    report["ended"].append(cluster_id)
                    if cluster_id not in (self.state.get("ended_chains") or []):
                        self.state["ended_chains"] = (self.state.get("ended_chains") or [])[-200:] + [cluster_id]
                        self._emit("chain_ended", cluster_id=cluster_id, draft_id=d["id"], why=why)
                    report["tidy"][cluster_id] = self._close_chain(d, chain)
                    report["closed"].append(cluster_id)
                    continue
                attempt = int(d.get("attempt", 1)) + 1
                out = self._mem.submit_draft({
                    "summary": f"redraft of {d['id']} (attempt {attempt})",
                    "operations": new_ops, "cluster_id": cluster_id, "attempt": attempt,
                    "terminal": attempt >= self._cfg.sleep.max_attempts,
                    "previous_draft_ids": list(d.get("previous_draft_ids") or []) + [d["id"]],
                    "brief_id_used": d.get("brief_id_used"),
                    "extra": self._stamp(),
                })
                self._save_replay(out.get("id"), attempt=attempt, brief_id=d.get("brief_id_used"),
                                  redraft_of=d["id"])
                report["resubmitted"].append({"draft_id": out.get("id"), "cluster_id": cluster_id,
                                              "attempt": attempt, "operations": len(new_ops)})
                self._log(f"resubmitted {len(new_ops)} operation(s) for {cluster_id} as attempt {attempt}")
                self._emit("tend_resubmitted", draft_id=out.get("id"), cluster_id=cluster_id,
                           attempt=attempt, operations=len(new_ops),
                           terminal=attempt >= self._cfg.sleep.max_attempts)
        except (MemoryError, Exception) as e:  # noqa: BLE001
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"tend error: {report['error']}")
        report["model_failures"] = list(self._model_failures)
        report["finished_at"] = time.time()
        self.state["last_tend"] = report
        self._remember_run("tend", report, resubmitted=len(report["resubmitted"]), ended=len(report["ended"]),
                           closed=len(report["closed"]), deferred=len(report["deferred"]))
        self._save_state()
        return report

    @staticmethod
    def _resolve_target_ops(denied: list[dict[str, Any]], d: dict[str, Any]) -> list[dict[str, Any]]:
        """A denied op that named target_op (a new_core in its draft): if that
        core was approved it exists now, and it is the op's target; if not,
        the op has no target and the redraft must find one (it may name the
        redraft of that core: see _link_redrafts)."""
        ops = d.get("operations") or []
        out = []
        for op in denied:
            t = op.get("target_op")
            if t is not None and not op.get("target_core_id"):
                core = ops[t] if isinstance(t, int) and 0 <= t < len(ops) else {}
                op = {**op, "target_core_id": core.get("long_term_id") if core.get("status") == "approved" else None,
                      "_target_op_denied": core.get("status") != "approved"}
            out.append(op)
        return out

    @staticmethod
    def _chain_cores(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The cores this chain has created so far: the right home for a
        denied op is often one approved beside it (hip-draft-deps). Nearest-
        core search may not rank a core minutes old, so they are shown."""
        made = []
        for x in chain:
            for op in x.get("operations") or []:
                if (op.get("status") == "approved" and op.get("long_term_id")
                        and op.get("kind") in ("new_core", "supersede", "verbatim")):
                    made.append({"id": op["long_term_id"], "topic": op.get("topic"),
                                 "content": op.get("edited_content") or op.get("content") or ""})
        return made

    REDRAFT_RULES = (
        "You are a memory consolidator's drafting worker. An operation you proposed on long-term memory "
        "was DENIED by the reviewer: the main model, whose memories these are, and who was there. THE "
        "CRITIQUE IS TRUE. Do not argue with it, soften it or explain it back: do exactly what it says.\n"
        "- If the critique gives the wording, the facts or a list of what to say, THAT is your content: "
        "write it out as the memory, complete.\n"
        "- If it names the kind (attach, new_core, supersede), use that kind.\n"
        "- If it names where the operation belongs - a core id, or another operation in this draft "
        "('op 7', 'target_op 7') - put it there: target_core_id for a core id from the lists below, "
        "exactly as given; \"target_op\": 7 for an operation the list below says is being redrafted.\n"
        "- If it says withdraw, a duplicate, already covered, or not true: withdraw.\n"
        "- Where the critique is silent, keep the kind and the target. A wrong target is not a reason to "
        "withdraw. new_core has no target. Never invent a core id.\n"
        "Return ONLY JSON, one of:\n"
        '{"kind": "new_core|attach|supersede", "target_core_id": "...", "content": "...", "rationale": "..."}\n'
        '{"withdraw": true, "rationale": "..."}\n'
        'EVERY ANSWER THAT IS NOT A WITHDRAWAL MUST HAVE "content": the text of the memory itself, as it '
        "should be kept. The rationale is one short sentence about what you changed; it is not the memory, "
        "and a memory written only there is lost.\n\n")

    @staticmethod
    def _ops_mentioned(critique: Optional[str]) -> list[int]:
        """Operations of the same draft the critique speaks of by number."""
        seen: list[int] = []
        for m in re.finditer(r"\b(?:target_op|ops?)\s*#?\s*(\d{1,3})\b", critique or "", flags=re.I):
            n = int(m.group(1))
            if n not in seen:
                seen.append(n)
        return seen

    def _redraft(self, denied: list[dict[str, Any]],
                 made: Optional[list[dict[str, Any]]] = None,
                 draft_ops: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        if not self.model_configured:
            self._log("denied operations need a model to redraft; none configured")
            return []
        shorts = {s["id"]: s for s in self._mem.shorts(limit=self._cfg.sleep.max_entries_per_run)}
        # The other operations of the draft, as a critique refers to them: 'attach
        # with target_op 7', 'the core op 8 just landed'. Approved ones are
        # cores now; denied ones are being redrafted beside this one.
        by_index = {int(o.get("index", i)): o for i, o in enumerate(draft_ops or [])}
        op_cores = {i: o["long_term_id"] for i, o in by_index.items()
                    if o.get("status") == "approved" and o.get("long_term_id")}
        pending = {int(o["index"]) for o in denied if o.get("index") is not None}
        out: list[dict[str, Any]] = []
        for op in denied:
            src = [shorts[i] for i in (op.get("source_short_ids") or []) if i in shorts]
            if op.get("kind") == "verbatim":
                if any((x.get("metadata") or {}).get("verbatim") for x in src):
                    # The reviewer marked it word for word, and then denied the
                    # operation. Its words are not the worker's to change, and
                    # the same words again would be denied again: it leaves
                    # the chain and the memory stays, still marked.
                    self._withdrawn += 1
                    self._log(f"operation {op.get('index')}: a verbatim the reviewer marked and then denied; "
                              "not reworded, left in short-term")
                    continue
                # chosen by an earlier worker, not marked by anyone: an ordinary operation
                op = {**op, "kind": "new_core"}
            # Approving an operation promotes its short-terms (Memory archives
            # them), and siblings often share them: approve op 4 and the denied
            # op 3 beside it loses its fragments. Seen live 27 Sept 2026 - the
            # redraft dropped op 3, and a chain whose every denied op was
            # orphaned could never be resubmitted OR closed. The critique and
            # the previous content are what a redraft works from; the fragments
            # were context. Redraft without them; the op keeps its source ids.
            if src:
                frag_lines = "\n".join(f"- [{s['id'][:8]}] {(s.get('content') or '')[:400]}" for s in src)
            else:
                frag_lines = ("- (none left: an approved operation in this draft already promoted them; "
                              "work from the previous content and the critique)")
            # The nearest real cores, as the draft side sees them. A redraft used
            # to keep the op's kind and target and only reword it, so an op aimed
            # at the wrong core came back aimed at the same core until the chain
            # was spent (seen live 27 Sept 2026: 'this does not supersede
            # 5504ed41', as a supersession of 5504ed41). It may now withdraw the
            # op, or change kind and target - to a core it was shown, never one
            # it invents.
            candidates = self._candidates(src + [{"content": op.get("content") or ""}])
            for c in made or []:                       # the cores this chain created, first
                if c["id"] not in {x["id"] for x in candidates}:
                    candidates.insert(0, dict(c))
            target = op.get("target_core_id")
            if target and target not in {c["id"] for c in candidates}:
                candidates.append({"id": target, "content": "(the core the denied operation targets)", "topic": None})
            # A CORE THE CRITIQUE NAMES IS A CORE TO SHOW. The reviewer wrote
            # 'attach to core 4e90f3c3...' and the redraft did - and was refused
            # for naming a core it had not been shown (six times on 2 Oct 2026).
            shown = {c["id"] for c in candidates}
            for cid in re.findall(r"\b[0-9a-f]{32}\b", op.get("critique") or ""):
                if cid not in shown:
                    core = self._mem.core(cid)
                    if core and (core.get("metadata") or {}).get("kind", "core") == "core":
                        candidates.append({"id": cid, "content": core.get("content") or "",
                                           "topic": (core.get("metadata") or {}).get("topic")})
                        shown.add(cid)
            mentioned = [n for n in self._ops_mentioned(op.get("critique")) if n != op.get("index")]
            sib_lines = []
            for n in mentioned:
                o = by_index.get(n)
                if o is None:
                    continue
                said = (o.get("edited_content") or o.get("content") or "")[:160]
                if n in op_cores:
                    sib_lines.append(f"op {n}: approved - it is core ({op_cores[n]}) now: {said}")
                    if op_cores[n] not in shown:
                        candidates.append({"id": op_cores[n], "content": o.get("edited_content") or o.get("content") or "",
                                           "topic": o.get("topic")})
                        shown.add(op_cores[n])
                elif n in pending:
                    sib_lines.append(f"op {n}: denied too and being redrafted now; to put this operation on the "
                                     f"core it becomes, answer \"target_op\": {n} (no target_core_id). It said: {said}")
            sib_block = ("Other operations in this draft that the critique mentions:\n" + "\n".join(sib_lines) + "\n\n"
                         if sib_lines else "")
            core_lines = self._core_lines(candidates)
            target_line = target or ("(none - it pointed at a new core in its draft that was not approved)"
                                     if op.get("_target_op_denied") else "(none)")
            prompt = (
                f"{self.voice.block()}{self.REDRAFT_RULES}"
                f"THE CRITIQUE:\n{op.get('critique')}\n\n"
                f"What you proposed (operation {op.get('index')}):\n"
                f"kind: {op.get('kind')}\ntarget core: {target_line}\ntopic: {op.get('topic') or 'untagged'}\n"
                f"content: {op.get('content')}\n\n"
                f"Source fragments (the critique may name them by the id in brackets):\n{frag_lines}\n\n"
                f"{sib_block}Existing cores:\n{core_lines}\n\n"
                f"The critique, once more: {op.get('critique')}\n\nJSON:")
            denied_op = {k: op.get(k) for k in ("index", "kind", "topic", "target_core_id", "content", "critique")}
            call: dict[str, Any] = {"stage": "redraft", "topic": op.get("topic"), "prompt": prompt,
                                    "entries": [{"id": s["id"], "content": s.get("content") or ""} for s in src],
                                    "candidates": candidates, "denied": denied_op,
                                    "op_cores": {str(k): v for k, v in op_cores.items()}, "pending": sorted(pending)}
            if self._capture is not None:
                self._capture.append(call)
            t0 = time.time()
            try:
                raw = self._call_model(prompt)
                if self._lacks_content(raw):
                    # The memory written into the rationale and "content" left out:
                    # seven of thirty redrafts on 2 Oct 2026, every one of them
                    # obeying its critique. Ask once more, saying exactly that.
                    call["first_answer"] = raw
                    raw = self._call_model(
                        prompt + raw + "\n\nThat answer has no \"content\", so there is nothing to keep. Give the "
                        "same answer again as JSON, with \"content\" holding the full text of the memory.\n\nJSON:")
            except ModelUnavailable:
                raise                                      # tend records it; the chain waits for the model
            except Exception as e:  # noqa: BLE001
                call.update(error=str(e), seconds=round(time.time() - t0, 2), ops=[])
                self._model_failure("redraft", op.get("topic"), str(e))
                continue
            call.update(answer=raw, seconds=round(time.time() - t0, 2))
            new = self._parse_redraft(raw, denied_op, call["entries"], candidates, op_cores, pending)
            if new is not None and new.get("withdrawn"):
                call.update(ops=[], withdrawn=True)
                self._withdrawn += 1
                self._log(f"operation {op.get('index')} withdrawn: {new.get('rationale') or 'no reason given'}")
                continue
            call["ops"] = [new] if new else []
            if new is None and not isinstance(self._json_from(raw), dict):
                call["error"] = "not the JSON asked for"
                self._model_failure("redraft", op.get("topic"), "the model's answer was not the JSON asked for: "
                                    + repr((raw or "")[:160]))
                continue
            if new is None and self._invented_target(raw, candidates, op_cores, pending):
                # A target it was not shown: a failure, like bad JSON - the next
                # tend asks again rather than landing a guess or ending the chain.
                call["error"] = "a target core it was not shown"
                self._model_failure("redraft", op.get("topic"), "the redraft named a core it was not shown")
                continue
            if new is None:
                # JSON, but no operation in it (a new_core with no text, say).
                # Not a failure: asked again it tends to answer the same, and a
                # chain that cannot move must end, not hold every sleep off. But
                # it used to vanish without a word (seen live 28 Sept 2026), so
                # say what was dropped.
                call["error"] = "no usable operation in the answer"
                self._log(f"operation {op.get('index')}: the redraft had no usable operation, dropped: "
                          + repr((raw or "")[:160]))
                continue
            if new and not src:
                # the fragments are gone (promoted by a sibling): the op keeps its ids as evidence
                new["source_short_ids"] = list(op.get("source_short_ids") or [])
                new["evidence_count"] = max(1, len(new["source_short_ids"]))
            if new:
                out.append(new)
        return self._submittable(self._link_redrafts(out))

    def _link_redrafts(self, out: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """An operation that asked to sit on the core ANOTHER redraft in this
        batch creates ('target_op 7', where op 7 was denied too): point it at
        that redraft's place in the new draft. If op 7 did not come back as a
        new core, the operation stands as a new core of its own - it is kept
        and the reviewer can place it - rather than being lost."""
        where = {o["redraft_of"]: i for i, o in enumerate(out)
                 if o.get("redraft_of") is not None and o["kind"] == "new_core"}
        for o in out:
            n = o.pop("_wants_op", None)
            if n is None:
                continue
            if n in where and out[where[n]] is not o:
                o["target_op"] = where[n]
            else:
                self._log(f"operation {o.get('redraft_of')}: op {n} did not come back as a new core; kept as a new core")
                o["kind"], o["target_core_id"] = "new_core", None
        return out

    def _lacks_content(self, raw: str) -> bool:
        parsed = self._json_from(raw)
        return (isinstance(parsed, dict) and parsed.get("withdraw") is not True
                and not str(parsed.get("content") or parsed.get("restated_content") or "").strip())

    @staticmethod
    def _target_of(parsed: dict[str, Any], candidates: list[dict[str, Any]],
                   op_cores: Optional[dict[int, str]] = None,
                   pending: Optional[set[int]] = None) -> tuple[Optional[str], Optional[int]]:
        """Where a redraft answer points: (core id, None), (None, the index of
        an operation being redrafted beside it), or (None, None) when it names
        nothing real. A core id may be given whole or as its first 8+
        characters, the way a critique writes it; an operation of the draft by
        number, as target_op or (seen live) as the target_core_id itself."""
        ids = [c["id"] for c in candidates]
        t = parsed.get("target_core_id")
        if isinstance(t, str):
            t = t.strip()
            if t in ids:
                return t, None
            if len(t) >= 8:
                near = [i for i in ids if i.startswith(t)]
                if len(near) == 1:
                    return near[0], None
        n: Optional[int] = None
        for v in (parsed.get("target_op"), t):
            if isinstance(v, bool):
                continue
            if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit() and len(v.strip()) <= 3):
                n = int(v)
                break
        if n is not None:
            if n in (op_cores or {}):
                return (op_cores or {})[n], None
            if n in (pending or set()):
                return None, n
        return None, None

    def _parse_redraft(self, raw: str, op: dict[str, Any], src: list[dict[str, Any]],
                       candidates: Optional[list[dict[str, Any]]] = None,
                       op_cores: Optional[dict[int, str]] = None,
                       pending: Optional[set[int]] = None) -> Optional[dict[str, Any]]:
        """A redraft answer turned into the operation that replaces the
        denied one; {"withdrawn": True, ...} when the worker drops it; None
        when there is nothing usable. Shared with a replay.

        The kind may change among new_core / attach / supersede (a verbatim op
        stays verbatim: its wording is the person's). The target may change to
        a core the worker was shown - the candidates, which include the old
        target and any core the critique named - or to another operation of
        the draft (see _target_of), and is None for new_core. A packet from
        before retargeting has no candidates: the old kind and target hold."""
        parsed = self._json_from(raw)
        if not isinstance(parsed, dict):
            return None
        rationale = str(parsed.get("rationale") or "")[:500] or None
        if parsed.get("withdraw") is True:
            return {"withdrawn": True, "rationale": rationale}
        kind = op.get("kind")
        target = op.get("target_core_id")
        wants: Optional[int] = None
        if candidates is not None and kind != "verbatim":
            asked = str(parsed.get("kind") or kind or "").strip().lower()
            if asked in ("new_core", "attach", "supersede"):
                kind = asked
            if kind == "new_core":
                target = None
            elif parsed.get("target_core_id") in (None, "") and parsed.get("target_op") is None:
                if target not in {c["id"] for c in candidates}:
                    return None                                # said nothing, and the old target is not a core shown
            else:
                target, wants = self._target_of(parsed, candidates, op_cores, pending)
                if wants is not None and wants == op.get("index"):
                    wants = None                               # itself is not a place to put it
                if target is None and wants is None:
                    return None                                # invented: see _invented_target
        content, cut = strip_scaffolding(str(parsed.get("content") or ""))
        if cut:
            self._log(f"operation {op.get('index')}: cut prompt scaffolding out of the redraft's text")
        if not content:
            # The text in restated_content instead. A new core or a supersession
            # has only the one statement, so that is it (seen live 28 Sept 2026:
            # a supersede with the whole corrected dream in restated_content).
            # An attach too: the episode put in the wrong field, with content
            # empty (1 Oct 2026) - it is the satellite's text.
            content = str(parsed.get("restated_content") or "").strip()
        if not content:
            return None
        # A REDRAFT NEVER REWORDS A CORE. restated_content replaces the target
        # core's wording when the attach is approved, and a redraft cannot be
        # trusted to write the whole core again. What it wrote there was the
        # episode's own text, and approving it overwrote two cores with their
        # satellites (1 Oct 2026). The attach lands as a satellite; the core
        # keeps its words.
        # A redraft used to cite every fragment the denied operation cited. It
        # carries only what its text covers (covered_fragments); the rest stays.
        keep = list(range(len(src)))
        if src and kind != "verbatim":
            keep, left = covered_fragments(content, keep, src)
            if left:
                self._log(f"operation {op.get('index')}: the redraft carries less than half of "
                          f"{len(left)} fragment(s); they stay in short-term: " + ", ".join(src[i]["id"][:8] for i in left))
        new = {"kind": kind, "content": content, "topic": op.get("topic"), "target_core_id": target,
               "source_short_ids": [src[i]["id"] for i in keep], "evidence_count": max(1, len(keep)),
               "rationale": rationale}
        if wants is not None:
            new["_wants_op"] = wants                       # resolved by _link_redrafts
        if op.get("index") is not None:
            new["redraft_of"] = op["index"]                # the reviewer sees every version of it (get_draft)
        return new

    def _invented_target(self, raw: str, candidates: list[dict[str, Any]],
                         op_cores: Optional[dict[int, str]] = None,
                         pending: Optional[set[int]] = None) -> bool:
        """The redraft named an attach / supersede target that is none of the
        cores it was shown and no operation of its draft."""
        parsed = self._json_from(raw)
        if not isinstance(parsed, dict) or parsed.get("withdraw") is True:
            return False
        if str(parsed.get("kind") or "").strip().lower() == "new_core":
            return False                                   # a new core has no target; a stray one is ignored
        if parsed.get("target_core_id") in (None, "") and parsed.get("target_op") is None:
            return False
        return self._target_of(parsed, candidates, op_cores, pending) == (None, None)

    # ── replay ────────────────────────────────────────────────────────────
    def _save_replay(self, draft_id: Optional[str], **fields: Any) -> None:
        calls, self._capture = self._capture, None
        if draft_id and calls:
            rp.save_packet(self._state_path(), draft_id, {"created_at": time.time(), "stamp": self._stamp(),
                                                          "calls": calls, **fields})

    def replay(self, draft_id: str, url: str = "", name: str = "", extra_body: Optional[dict] = None,
               timeout_seconds: Optional[int] = None) -> dict[str, Any]:
        """The same prompts a draft was built from, sent to a candidate model
        (url; empty = the configured one), parsed and validated exactly as a
        sleep would. Nothing is submitted to Memory. See replay.py."""
        pk = rp.load_packet(self._state_path(), draft_id)
        if pk is None:
            raise KeyError(f"no replay packet for draft '{draft_id}' - drafts from before replays, or pruned")
        m = self._cfg.model
        url = url.strip() or m.url
        eb = m.extra_body if extra_body is None else extra_body
        timeout = int(timeout_seconds or m.timeout_seconds)
        cand_calls: list[dict[str, Any]] = []
        served = ""
        for c in pk.get("calls") or []:
            out: dict[str, Any] = {"stage": c.get("stage"), "topic": c.get("topic"),
                                   "entries": c.get("entries"), "candidates": c.get("candidates")}
            t0 = time.time()
            try:
                if url == m.url and not url.startswith("replay-test:"):
                    raw = self._call_model(c["prompt"])
                    got = self._served_model
                else:
                    raw, got = self._call_at(url, name or m.name, c["prompt"], m.max_tokens, eb, timeout)
                served = got or served
                out["answer"] = raw
                if c.get("stage") == "redraft":
                    new = self._parse_redraft(raw, c.get("denied") or {}, c.get("entries") or [],
                                              c.get("candidates"))
                    if new is not None and new.get("withdrawn"):
                        out.update(ops=[], withdrawn=True)
                    else:
                        out["ops"] = [new] if new else []
                else:
                    ops = self._parse_draft(raw, c.get("topic"), c.get("entries") or [], c.get("candidates") or [])
                    out["ops"] = ops or []
                    if ops is None:
                        out["error"] = "not the JSON asked for"
            except Exception as e:  # noqa: BLE001 - a candidate that fails is a result, not a crash
                out.update(ops=[], error=f"{type(e).__name__}: {e}")
            out["seconds"] = round(time.time() - t0, 2)
            cand_calls.append(out)
        # what the original got from the reviewer, and what finally landed
        try:
            chain = self._mem.draft_chain(draft_id)
            original = next((d for d in chain if d.get("id") == draft_id), None)
        except Exception:  # noqa: BLE001
            chain, original = [], None
        landed = rp.landed_texts(chain)
        orig_calls = pk.get("calls") or []
        return {
            "draft_id": draft_id, "attempt": pk.get("attempt"), "created_at": pk.get("created_at"),
            "landed": landed,
            "original": {"model": pk.get("stamp"), "calls": orig_calls,
                         "reviewed_ops": (original or {}).get("operations") or [],
                         "checks": rp.check_side(orig_calls, landed)},
            "candidate": {"url": url, "served": served, "calls": cand_calls,
                          "checks": rp.check_side(cand_calls, landed)},
        }
