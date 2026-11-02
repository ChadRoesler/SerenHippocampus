"""
Lessons: the consolidator's training set, made from the reviews.

Every night the small model drafts and the reviewer approves, edits or
denies; what finally landed is the right answer to the prompt the small
model saw. This turns those into examples a training can learn from - the
Lyceum imports them as a dataset ("From the hippocampus").

Each example: the CURRENT aging-flow prompt (rendered by this hippocampus's
own _aging_prompt, so the text is exactly what the model sees at night) for
one drafting call's memories and cores, and as the answer, what actually
happened to each memory - support / refine / replace / new_core as approved
(with the reviewer's edits), age_out for the ones that were let go.
Rationales are short neutral templates: the model's own were often invented,
and that is not a habit to teach.

Skipped, and counted in the report: calls whose landed target is a core the
call was not shown (the answer would teach inventing ids), calls with a
memory still in short-term (not decided yet - it is not an age_out), and
calls whose memory set repeats a later call's (the old flow re-drafted free
short-terms every sleep). Redraft calls are not read: another prompt.

Read-only everywhere: Memory over its API, the replay packets from disk.
Built on the 23 Oct 2026 hand run that trained consolidator v1.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .replay import packet_dir

WHY = {"support": "More evidence for this core; the memory is kept under it.",
       "refine": "Supports the core and changes a detail of it.",
       "replace": "Shows the core was wrong where it says what is quoted.",
       "new_core": "No core covers this and it is worth keeping.",
       "age_out": "A one-off with nowhere to go."}


def consolidator_set(h: Any, mem: Any, recover: bool = False, redrafts: bool = False,
                     keys: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(rows, report). rows are {"messages": [user prompt, assistant answer]}.

    Off by default, so the Lyceum's plain pull stays what it was (1 Nov 2026):
    - recover: a call whose landed core was not shown to the model is kept, with
      that core added to the cores it is shown (the night now shows the cores a
      memory orbits first, so the right core is usually there).
    - redrafts: each redraft the reviewer saw becomes a lesson too: today's redraft
      instructions over the saved body (the critique, what was proposed, the
      fragments, the cores), answered with what finally landed, in the aging flow's
      terms. A redraft whose landing cannot be named from what it was shown is left out.
    - keys: each row carries "key" (its memories' ids, sorted) and "source"
      (first_draft, recovered, redraft), so a training can be kept apart from its exam."""
    bar = h._cfg.sleep.promote_min_evidence
    drafts = mem.drafts(limit=100000)
    fate: dict[str, tuple[dict, dict]] = {}    # short id -> (draft, op) of the approved op that consumed it
    for d in sorted(drafts, key=lambda d: d.get("created_at") or 0):
        for op in d.get("operations", []):
            if op.get("status") != "approved":
                continue
            for sid in op.get("source_short_ids") or []:
                fate[sid] = (d, op)
    # a fragment's own date survives on the satellite it became
    born: dict[str, Any] = {}
    for e in mem.long_entries(include_satellites=True, include_superseded=True):
        m = e.get("metadata") or {}
        for sid in m.get("source_short_ids") or []:
            if m.get("kind") == "satellite" and m.get("created_at"):
                born.setdefault(sid, m["created_at"])
    alive = {e["id"] for e in mem.shorts(limit=100000)}

    calls = []
    d = packet_dir(h._state_path())
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in p.get("calls", []):
            if c.get("stage") in ("draft", "decide") and c.get("entries"):
                calls.append((p.get("created_at") or 0, c))
    calls.sort(key=lambda t: -t[0])            # newest first, so a repeat keeps the latest

    seen: set = set()
    rows: list[dict[str, Any]] = []
    why_skip: Counter = Counter()
    kinds: Counter = Counter()
    for _, c in calls:
        entries = c["entries"]
        key = tuple(sorted(e["id"] for e in entries))
        if key in seen:
            why_skip["repeat of a later call"] += 1
            continue
        seen.add(key)
        cands = c.get("candidates") or []
        shown = {k["id"] for k in cands}
        groups: dict[str, list[int]] = defaultdict(list)     # landed op id -> entry indexes
        landed: dict[str, tuple[dict, dict]] = {}
        undecided = False
        for i, e in enumerate(entries):
            hit = fate.get(e["id"])
            if hit is None:
                if e["id"] in alive:           # still in short-term: not decided yet, not an age_out
                    undecided = True
                    break
                groups["age_out"].append(i)
                continue
            dr, op = hit
            k = f"{dr['id']}:{op['index']}"
            groups[k].append(i)
            landed[k] = (dr, op)
        if undecided or not groups:
            why_skip["a memory in it is still in short-term (undecided)"] += 1
            continue
        ops: list[dict[str, Any]] = []
        bad = None
        recovered = False
        new_pos: dict[str, int] = {}
        for k, idxs in groups.items():
            if k == "age_out":
                ops.append({"kind": "age_out", "aging": idxs, "rationale": WHY["age_out"]})
                continue
            dr, op = landed[k]
            content = op.get("edited_content") or op.get("content") or ""
            kind = op.get("kind")
            if kind in ("new_core", "verbatim"):
                new_pos[op.get("long_term_id")] = len(ops)
                ops.append({"kind": "new_core", "aging": idxs, "content": content, "rationale": WHY["new_core"]})
            elif kind == "age_out":
                ops.append({"kind": "age_out", "aging": idxs, "rationale": WHY["age_out"]})
            elif kind in ("attach", "supersede"):
                target = op.get("target_core_id") or op.get("long_term_id")
                o: dict[str, Any] = {"aging": idxs}
                if target in shown:
                    o["target_core_id"] = target
                elif target in new_pos:
                    o["target_op"] = new_pos[target]
                elif recover and target and (core := mem.core(target)) \
                        and (core.get("metadata") or {}).get("kind", "core") == "core":
                    cands = cands + [{"id": target, "content": core.get("content") or "",
                                      "topic": (core.get("metadata") or {}).get("topic")}]
                    shown.add(target)
                    recovered = True
                    o["target_core_id"] = target
                else:
                    bad = "the landed core was not shown to the model"
                    break
                if kind == "supersede" and (op.get("contradicts") or ""):
                    o.update(kind="replace", content=content, contradicts=op["contradicts"])
                elif kind == "attach" and (op.get("restated_content") or "").strip():
                    o.update(kind="refine", restated_content=op["restated_content"].strip())
                else:
                    o["kind"] = "support"
                o["rationale"] = WHY[o["kind"]]
                ops.append({"kind": o.pop("kind"), **o})
            else:
                bad = f"landed as {kind}"
                break
        if bad:
            why_skip[bad] += 1
            continue
        allowed_new = any(o["kind"] == "new_core" for o in ops) or len(entries) >= bar
        dated = [{**e, "metadata": {**(e.get("metadata") or {}), **({"ts": born[e["id"]]} if e["id"] in born else {})}}
                 for e in entries]
        prompt = h._aging_prompt(c.get("topic"), dated, [], cands, len(entries), allowed_new, "")
        answer = json.dumps({"operations": ops}, ensure_ascii=False)
        row = {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": answer}]}
        if keys:
            row["key"] = sorted(e["id"] for e in entries)
            row["source"] = "recovered" if recovered else "first_draft"
        rows.append(row)
        kinds.update(o["kind"] for o in ops)
        if recovered:
            why_skip["recovered (landed core added to the shown cores)"] += 1
    if redrafts:
        rrows, rreport = _redraft_lessons(h, mem, fate, alive, keys)
        rows += rrows
        why_skip.update({f"redraft: {k}": v for k, v in rreport["skipped"].items()})
        kinds.update({f"redraft {k}": v for k, v in rreport["kinds"].items()})
    lens = sorted(len(r["messages"][0]["content"]) + len(r["messages"][1]["content"]) for r in rows)
    report = {"examples": len(rows), "calls_read": len(calls), "skipped": dict(why_skip),
              "operations_by_kind": dict(kinds), "dated_fragments": len(born),
              "chars_median": lens[len(lens) // 2] if lens else 0, "chars_max": lens[-1] if lens else 0,
              "say": _say(len(rows), kinds)}
    return rows, report


def _redraft_lessons(h: Any, mem: Any, fate: dict, alive: set, keys: bool):
    """A redraft is the critique made into a lesson: what the reviewer said was
    wrong, and what finally landed. Today's instructions head the saved body, and
    the answer is given in the aging flow's terms whichever flow it came from."""
    preamble = f"{h.voice.block()}{h.REDRAFT_RULES}{h.REDRAFT_AGING}"
    d = packet_dir(h._state_path())
    calls = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            pk = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in pk.get("calls", []):
            if c.get("stage") == "redraft" and c.get("prompt") and (c.get("denied") or {}).get("critique"):
                calls.append((pk.get("created_at") or 0, c))
    calls.sort(key=lambda t: -t[0])            # newest first: a later redraft of the same memories wins
    rows: list[dict[str, Any]] = []
    skip: Counter = Counter()
    kinds: Counter = Counter()
    seen: set = set()
    for _, c in calls:
        body_at = c["prompt"].find("THE CRITIQUE:")
        if body_at < 0:
            skip["no critique block in the saved prompt"] += 1
            continue
        ids = sorted(e["id"] for e in c.get("entries") or [])
        if not ids:
            skip["its fragments were already promoted (nothing to land)"] += 1
            continue
        key = (tuple(ids), (c.get("denied") or {}).get("critique"))
        if key in seen:
            skip["repeat of a later redraft"] += 1
            continue
        seen.add(key)
        landed = {f"{fate[i][0]['id']}:{fate[i][1]['index']}" for i in ids if i in fate}
        if any(i in alive for i in ids):
            skip["a memory in it is still in short-term (undecided)"] += 1
            continue
        if not landed:
            if not c.get("aging"):
                skip["old flow, never landed (not an age-out decision then)"] += 1
                continue
            ans = {"kind": "age_out", "rationale": WHY["age_out"]}
        elif len(landed) > 1:
            skip["its memories landed in different places"] += 1
            continue
        else:
            dr, op = fate[ids[0]]
            kind = op.get("kind")
            content = op.get("edited_content") or op.get("content") or ""
            shown = {x["id"] for x in c.get("candidates") or []} | set((c.get("op_cores") or {}).values())
            if kind in ("new_core", "verbatim"):
                ans = {"kind": "new_core", "content": content, "rationale": WHY["new_core"]}
            elif kind in ("attach", "supersede"):
                target = op.get("target_core_id") or op.get("long_term_id")
                if target not in shown:
                    skip["landed on a core this redraft was not shown"] += 1
                    continue
                if kind == "supersede" and (op.get("contradicts") or ""):
                    ans = {"kind": "supersede", "target_core_id": target, "content": content,
                           "contradicts": op["contradicts"], "rationale": WHY["replace"]}
                else:
                    ans = {"kind": "attach", "target_core_id": target, "rationale": WHY["support"]}
            else:
                skip[f"landed as {kind}"] += 1
                continue
        row = {"messages": [{"role": "user", "content": preamble + c["prompt"][body_at:]},
                            {"role": "assistant", "content": json.dumps(ans, ensure_ascii=False)}]}
        if keys:
            row["key"] = ids
            row["source"] = "redraft"
        rows.append(row)
        kinds[ans["kind"]] += 1
    return rows, {"skipped": dict(skip), "kinds": dict(kinds)}


def _say(n: int, kinds: Counter) -> str:
    if not n:
        return "No lessons yet: nothing the small model drafted has been decided."
    parts = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in kinds.most_common())
    return (f"{n} lessons from the reviews: what the small model was shown, and what landed. "
            f"The answers hold {parts}.")


def to_jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
