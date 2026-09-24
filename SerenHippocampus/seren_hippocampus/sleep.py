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

OP_KINDS = ("new_core", "attach", "supersede", "verbatim")


class Busy(RuntimeError):
    """A sleep or tend is already running in this process."""


class Hippocampus:
    def __init__(self, cfg: HippocampusConfig, memory: MemoryClient, log=None):
        self._cfg = cfg
        self._mem = memory
        self._log = log or (lambda m: print(f"[seren-hippocampus] {m}"))
        self._lock = threading.Lock()
        self.state: dict[str, Any] = self._load_state()

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
        url = m.url.rstrip("/") + "/chat/completions"
        payload = {"model": m.name, "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": max_tokens or m.max_tokens, "temperature": 0.2}
        with httpx.Client(timeout=m.timeout_seconds) as c:
            r = c.post(url, json=payload)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""

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
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._sleep()
        finally:
            self._lock.release()

    def tend(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise Busy("a sleep or tend is already running")
        try:
            return self._tend()
        finally:
            self._lock.release()

    # ── sleep ─────────────────────────────────────────────────────────────
    def _sleep(self) -> dict[str, Any]:
        start = time.time()
        report: dict[str, Any] = {"started_at": start, "purged": 0, "brief_id": None,
                                  "brief_pulled": False, "clusters": 0, "operations": 0,
                                  "docket_id": None, "held_back": 0, "tidy": {}, "error": None}
        try:
            # 1. what was flagged
            purged = self._mem.tidy(age_out=False, near=False, sweep=False, purge=True).get("purged") or []
            report["purged"] = len(purged)
            if purged:
                self._log(f"purged {len(purged)} flagged memories: " +
                          ", ".join(t.get("id", "?") for t in purged))

            # 2. the brief
            brief = self._fresh_brief()
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
                ops += self._propose(topic, entries, promote_hints, noise_hints)
            report["operations"] = len(ops)
            if ops:
                summary = self._summarise(ops)
                out = self._mem.submit_docket({"summary": summary, "operations": ops,
                                               "brief_id_used": report["brief_id"], "attempt": 1})
                report["docket_id"] = out.get("id")
                self._log(f"submitted docket {out.get('id')} with {len(ops)} operation(s)")

            # 4. tidy
            report["tidy"] = self._mem.tidy(age_out=True, near=True, sweep=True, purge=False)
        except (MemoryError, Exception) as e:  # noqa: BLE001 - a sleep records its failure and ends
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"sleep error: {report['error']}")
        report["finished_at"] = time.time()
        report["duration_seconds"] = round(report["finished_at"] - start, 2)
        self.state["last_sleep"] = report
        self._save_state()
        return report

    def _held_short_ids(self) -> set[str]:
        """Short-terms already spoken for by a pending docket. Proposing them
        again every sleep is how the old consolidator made duplicate drafts."""
        held: set[str] = set()
        for d in self._mem.dockets(status="pending"):
            for op in d.get("operations") or []:
                if op.get("status", "pending") == "pending":
                    held.update(str(x) for x in (op.get("source_short_ids") or []))
        return held

    def _fresh_brief(self) -> Optional[dict[str, Any]]:
        latest = self._mem.latest_brief()
        last = (self.state.get("last_sleep") or {}).get("finished_at") or 0
        if latest and float((latest.get("metadata") or {}).get("created_at", 0) or 0) > float(last):
            return latest
        return self._pull_brief()

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
                 promote_hints: set[str], noise_hints: set[str]) -> list[dict[str, Any]]:
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
        if any(h and h in haystack for h in promote_hints):
            threshold = 1
        if any(h and h in haystack for h in noise_hints):
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
        drafted = self._draft_cluster(real_topic, remaining, candidates)
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

    def _draft_cluster(self, topic: Optional[str], entries: list[dict[str, Any]],
                       candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        frag_lines = "\n".join(f"[{i}] {(e.get('content') or '')[:400]}" for i, e in enumerate(entries))
        core_lines = "\n".join(f"({c['id']}) {c['content'][:300]}" for c in candidates) or "(none)"
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
            "operations beat many.\n\n"
            f"Topic: {topic or 'untagged'}\n\nFragments:\n{frag_lines}\n\nExisting cores:\n{core_lines}\n\nJSON:")
        try:
            raw = self._call_model(prompt)
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
                                  "ended": [], "error": None}
        try:
            reviewed = self._mem.dockets(status="reviewed")
            report["examined"] = len(reviewed)
            for d in reviewed:
                denied = [op for op in (d.get("operations") or []) if op.get("status") == "denied"]
                if not denied:
                    continue
                cluster_id = d.get("cluster_id") or d["id"]
                chain = self._mem.docket_chain(d["id"])
                if any(int(x.get("attempt", 1)) > int(d.get("attempt", 1)) for x in chain):
                    continue                                   # already resubmitted
                if d.get("terminal"):
                    report["ended"].append(cluster_id)         # the chain is spent; shorts stay
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
        except (MemoryError, Exception) as e:  # noqa: BLE001
            report["error"] = f"{type(e).__name__}: {e}"
            self._log(f"tend error: {report['error']}")
        report["finished_at"] = time.time()
        self.state["last_tend"] = report
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
