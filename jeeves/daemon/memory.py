"""Long-term (RAM) and permanent (disk) memory."""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from .. import paths
from ..util import normalize

LONG_TERM_PHRASES = ["remember for a while", "commit to long term memory", "commit to long-term memory"]
PERMANENT_PHRASES = ["remember forever", "commit to permanent memory"]


class Memory:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.long_term: list[dict[str, Any]] = []
        self.file = paths.data_dir() / "permanent_memory.json"
        self.permanent: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.permanent, indent=2))

    def add(self, text: str, permanent: bool = False, agent: str | None = None) -> None:
        note = {"text": text.strip(), "time": time.time(), "agent": agent, "permanent": permanent}
        with self._lock:
            if permanent:
                self.permanent.append(note)
                self._save()
            else:
                self.long_term.append(note)
                limit = int(self.settings.get("memory.long_term_limit", 200))
                del self.long_term[:-limit]

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.permanent) + list(self.long_term)

    def search(self, query: str) -> list[dict[str, Any]]:
        q = normalize(query)
        notes = self.all()
        if not q:
            return notes
        words = set(q.split())
        scored = []
        for n in notes:
            nw = set(normalize(n["text"]).split())
            overlap = len(words & nw)
            if overlap or q in normalize(n["text"]):
                scored.append((overlap, n))
        return [n for _, n in sorted(scored, key=lambda x: -x[0])]

    def forget(self, query: str) -> int:
        q = normalize(query)
        with self._lock:
            before = len(self.permanent) + len(self.long_term)
            self.permanent = [n for n in self.permanent if q not in normalize(n["text"])]
            self.long_term = [n for n in self.long_term if q not in normalize(n["text"])]
            self._save()
            return before - len(self.permanent) - len(self.long_term)

    def notes_for(self, agent: str | None = None, limit: int = 30, own_only: bool = False) -> list[dict[str, Any]]:
        notes = self.all()
        if own_only:
            notes = [n for n in notes if n.get("agent") == agent]
        return notes[-limit:] if limit > 0 else []

    def context_for(self, agent: str | None = None, limit: int = 30, own_only: bool = False, about: str = "",
                    max_chars: int = 1500) -> str:
        """The notes for a reply. With many of them, the ones that have to do with what was just said
        (`about`) and the newest few -- every note on every reply made small models bring up things
        nobody asked about."""
        notes = self.notes_for(agent, limit, own_only)
        if not notes:
            return ""
        if len(notes) > 8 or sum(len(n["text"]) for n in notes) > max_chars:
            words = {w for w in normalize(about).split() if len(w) > 2}
            newest = {id(n) for n in notes[-3:]}
            ranked = sorted(notes, key=lambda n: (-len(words & set(normalize(n["text"]).split())),
                                                  id(n) not in newest, -n.get("time", 0)))
            keep, total = set(), 0
            for n in ranked:
                if total + len(n["text"]) > max_chars or len(keep) >= 12:
                    break
                if id(n) in newest or words & set(normalize(n["text"]).split()):
                    keep.add(id(n))
                    total += len(n["text"]) + 3
            notes = [n for n in notes if id(n) in keep]
        if not notes:
            return ""
        return ("Things the user asked you to remember (use them only when they matter to what they say now):\n"
                + "\n".join(f"- {n['text']}" for n in notes))

    @staticmethod
    def detect(text: str) -> tuple[str | None, str]:
        """('long_term'|'permanent'|None, text without the phrase)."""
        low = text.lower()
        for kind, phrases in (("permanent", PERMANENT_PHRASES), ("long_term", LONG_TERM_PHRASES)):
            for p in phrases:
                i = low.find(p)
                if i >= 0:
                    cleaned = (text[:i] + text[i + len(p):]).strip(" ,.:;-")
                    return kind, cleaned
        return None, text
