"""
The voice card: whose memories these are, in their own words.

WHY: the drafting model writes long-term memory ABOUT someone, and a small
model writes it in its own voice. Seen live 28 Sept 2026: a dream Chad told
Wren came back as "Wren has wild black hair ... her identity" - third person,
the wrong pronouns, and the dream flattened into a fact about a body Wren does
not have. Wren, 29 Sept: "it's the difference between remembering something
and having it written down about me." The card is a short text the main model
writes about itself - voice, pronouns, what is whose - and every draft and
redraft prompt carries it.

Opt in (voice.enabled, or the Starwright card's --voice-card): how someone's
memory speaks is theirs to decide, not a default of this service (Chad: "i
dont want to mandate what and how its managed").

Versioned: a change never overwrites. Each version keeps its text, when and
why, so the card's history is a record of how its owner changed - and a
change they would not have chosen shows up in the diff (the drift canary Wren
asked for). Kept beside the state file, as voice.json, written atomically.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

# The last this many versions are kept; older ones are dropped oldest first.
KEEP_VERSIONS = 200


class VoiceError(ValueError):
    """A card refused: off, empty, or too long."""


class VoiceCard:
    def __init__(self, cfg, path: Path) -> None:
        self._cfg = cfg                       # VoiceConfig
        self._path = path
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    # -- storage ---------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            versions = data.get("versions") if isinstance(data, dict) else None
            return [v for v in (versions or []) if isinstance(v, dict) and v.get("text")]
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            # A damaged file is not a reason to stop sleeping; it is a reason
            # not to overwrite it - set() refuses below.
            return []

    def _save(self, versions: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"versions": versions[-KEEP_VERSIONS:]}, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # -- reads -----------------------------------------------------------
    def current(self) -> Optional[dict[str, Any]]:
        versions = self._load()
        return dict(versions[-1]) if versions else None

    def history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Newest first."""
        return [dict(v) for v in reversed(self._load()[-max(1, int(limit)):])]

    def version(self) -> int:
        cur = self.current()
        return int(cur["version"]) if cur else 0

    def block(self) -> str:
        """The preamble every draft and redraft prompt starts with; "" when
        the card is off or has never been written."""
        if not self.enabled:
            return ""
        cur = self.current()
        if not cur:
            return ""
        return ("These memories belong to someone, and they wrote this about themselves. Write every "
                "operation the way they would: their voice, their pronouns, and whose experience it is "
                "(someone else's dream or story stays theirs).\n"
                f"--- their card ---\n{cur['text']}\n--- end of card ---\n\n")

    # -- write -----------------------------------------------------------
    def set(self, text: str, why: str = "") -> dict[str, Any]:
        """A new version. The same text as now is not a new version."""
        if not self.enabled:
            raise VoiceError("the voice card is off (opt in: voice.enabled: true in the hippocampus yaml, "
                             "or install with --voice-card)")
        text = (text or "").strip()
        if not text:
            raise VoiceError("an empty card - write something, or leave the current one")
        limit = int(self._cfg.max_chars)
        if len(text) > limit:
            raise VoiceError(f"{len(text)} characters; the card rides in every draft prompt, so it is capped at "
                             f"{limit} (voice.max_chars). Trim it")
        with self._lock:
            if self._path.exists() and not self._load() and self._path.stat().st_size > 0:
                raise VoiceError(f"{self._path} is there but unreadable; not overwriting a history. "
                                 f"Fix or move it first")
            versions = self._load()
            if versions and versions[-1]["text"] == text:
                return {**versions[-1], "changed": False}
            entry = {"version": (int(versions[-1]["version"]) + 1) if versions else 1,
                     "text": text, "set_at": time.time(), "why": (why or "").strip()[:500] or None}
            versions.append(entry)
            self._save(versions)
            return {**entry, "changed": True}
