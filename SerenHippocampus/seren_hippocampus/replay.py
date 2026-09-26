"""
Replay: the same inputs, another model, side by side.

Chad, 26 Sept 2026: "lets hit that next piece of a replay, so that we can
validate when one seems better." The audit says how a model did over weeks
of real sleeps; a replay answers the sharper question - given EXACTLY what
the last model saw, what would this one have proposed?

Every draft and redraft saves its model calls as a replay packet in the
state folder (replays/<draft_id>.json): the prompt, the fragments and cores
it was built from, the answer, and the operations that came of it. The
packet is the input as it was, which matters because the fragments behind a
draft get archived and later swept; rebuilding the prompt afterwards would
replay something else.

A replay sends each saved prompt to a candidate model and runs the answer
through the SAME parser and validator a real sleep uses. Nothing is
submitted to Memory: a replay cannot change a memory. It comes back with
both sides, the verdicts the original got, what finally landed in the
chain, and plain checks on each side:

  valid              operations that survived validation
  failed_calls       answers that were not the JSON asked for, or errors
  invented_numbers   numbers and dates in the content that none of the
                     fragments or cores contain (last night's "2026-09-23")
  overlap_landed     how close the content is to what finally landed
                     (word overlap, best match per operation, averaged)
  seconds            wall time of the candidate's calls
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

KEEP = 300                               # replay packets kept, newest first
_NUM = re.compile(r"\d[\d:./-]*\d|\d")
_WORD = re.compile(r"[a-z0-9']+")


def packet_dir(state_path: Path) -> Path:
    return state_path.parent / "replays"


def save_packet(state_path: Path, draft_id: str, packet: dict[str, Any]) -> Optional[Path]:
    d = packet_dir(state_path)
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{draft_id}.json"
        f.write_text(json.dumps({"draft_id": draft_id, **packet}, indent=1), encoding="utf-8")
        old = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP:]
        for p in old:
            p.unlink(missing_ok=True)
        return f
    except OSError:
        return None                      # a packet is a convenience; the draft is the record


def load_packet(state_path: Path, draft_id: str) -> Optional[dict[str, Any]]:
    f = packet_dir(state_path) / f"{Path(draft_id).name}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_packets(state_path: Path, limit: int = 50) -> list[dict[str, Any]]:
    out = []
    files = sorted(packet_dir(state_path).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:limit]:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"draft_id": p.get("draft_id"), "attempt": p.get("attempt"), "created_at": p.get("created_at"),
                    "calls": len(p.get("calls") or []), "model": (p.get("stamp") or {}).get("model_served")
                    or (p.get("stamp") or {}).get("model_name")})
    return out


# ── the checks ────────────────────────────────────────────────────────────────
def _source_text(call: dict[str, Any]) -> str:
    parts = [e.get("content") or "" for e in call.get("entries") or []]
    parts += [c.get("content") or "" for c in call.get("candidates") or []]
    return " ".join(parts)


def invented_numbers(content: str, source: str) -> list[str]:
    have = set(_NUM.findall(source or ""))
    return sorted({n for n in _NUM.findall(content or "") if n not in have})


def _words(t: str) -> set[str]:
    return set(_WORD.findall((t or "").lower()))


def overlap(content: str, landed: list[str]) -> Optional[float]:
    if not landed:
        return None
    w = _words(content)
    best = 0.0
    for l in landed:
        lw = _words(l)
        if w or lw:
            best = max(best, len(w & lw) / len(w | lw))
    return round(best, 3)


def check_side(calls: list[dict[str, Any]], landed: list[str]) -> dict[str, Any]:
    ops = [(op, c) for c in calls for op in (c.get("ops") or [])]
    inv: list[str] = []
    for op, c in ops:
        inv += invented_numbers(op.get("content") or "", _source_text(c))
    ov = [overlap(op.get("content") or "", landed) for op, _ in ops]
    ov = [x for x in ov if x is not None]
    return {"valid": len(ops), "failed_calls": sum(1 for c in calls if c.get("error")),
            "invented_numbers": sorted(set(inv)),
            "overlap_landed": round(sum(ov) / len(ov), 3) if ov else None,
            "seconds": round(sum(c.get("seconds") or 0 for c in calls), 2)}


def landed_texts(chain: list[dict[str, Any]]) -> list[str]:
    return [op.get("edited_content") or op.get("content") or ""
            for d in chain for op in (d.get("operations") or []) if op.get("status") == "approved"]


def now() -> float:
    return time.time()
