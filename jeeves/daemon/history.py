"""Response history: every request, what the agent did (the full trace), the
answer, and the user's rating/comment. Feeds Manual Response Review, the
History page, Memory and the intent model's rated examples."""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from .. import paths


def new_entry(text: str, agent: str | None, source: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12], "time": time.time(), "text": text, "agent": agent, "source": source,
        "function": None, "args": None, "confidence": None, "response": "", "status": "running",
        "trace": [], "rating": None, "comment": "", "should_use": None, "dry_run": False,
    }


class History:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.file = paths.data_dir() / "history.jsonl"
        self.entries: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        try:
            with open(self.file) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        out[e["id"]] = e
                    except (ValueError, KeyError):
                        continue
        except OSError:
            return []
        return sorted(out.values(), key=lambda e: e["time"])

    def _persist_all(self) -> None:
        limit = int(self.settings.get("general.history_limit", 500))
        self.entries = self.entries[-limit:]
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            for e in self.entries:
                f.write(json.dumps(e, default=str) + "\n")
        tmp.replace(self.file)

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.entries.append(entry)

    def finish(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if entry.get("dry_run"):
                return
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            limit = int(self.settings.get("general.history_limit", 500))
            if len(self.entries) > limit * 1.5:
                self._persist_all()

    def get(self, entry_id: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def list(self, offset: int = 0, limit: int = 50, agent: str | None = None) -> list[dict[str, Any]]:
        items = [e for e in reversed(self.entries) if not e.get("dry_run") and (agent is None or e["agent"] == agent)]
        return items[offset: offset + limit]

    def recent(self, n: int, agent: str | None = None, exclude: str | None = None, viewer: str | None = None,
               can_hear: Any = None) -> list[dict[str, Any]]:
        """Newest last. viewer + can_hear(source): skip other agents' requests from sources the
        viewer can't hear."""
        out = []
        for e in reversed(self.entries):
            if e.get("dry_run") or e["id"] == exclude or (agent and e["agent"] != agent):
                continue
            if can_hear is not None and e["agent"] != viewer and not can_hear(e.get("source") or "text"):
                continue
            out.append({"text": e["text"], "agent": e["agent"], "function": e["function"], "args": e["args"],
                        "result": e["response"], "time": e["time"]})
            if len(out) >= n:
                break
        return list(reversed(out))

    def rate(self, entry_id: str, rating: int | None, comment: str = "", should_use: str | None = None) -> dict:
        with self._lock:
            e = self.get(entry_id)
            if e is None:
                raise KeyError(entry_id)
            e["rating"], e["comment"], e["should_use"] = rating, comment, should_use or None
            self._persist_all()
            return e

    def rated_examples(self, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
        """function -> [{text, good, comment, should_use}] for the Dictionary."""
        out: dict[str, list[dict[str, Any]]] = {}
        count = 0
        for e in reversed(self.entries):
            if e.get("rating") is None or not e.get("function"):
                continue
            ex = {"text": e["text"], "good": e["rating"] > 0, "comment": e.get("comment", ""),
                  "should_use": e.get("should_use")}
            out.setdefault(e["function"], []).append(ex)
            if e.get("should_use") and e["should_use"] != e["function"]:
                out.setdefault(e["should_use"], []).append({"text": e["text"], "good": True,
                                                             "comment": "the user says this should use it"})
            count += 1
            if count >= limit:
                break
        return out
