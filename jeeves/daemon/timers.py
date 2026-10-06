"""Timers and schedules. Shown by the overlay in the bottom-right corner."""
from __future__ import annotations

import datetime as _dt
import threading
import time
import uuid
from typing import Any, Callable


class Timers:
    def __init__(self, on_fire: Callable[[dict[str, Any]], None], publish: Callable[[str, Any], None]) -> None:
        self.on_fire = on_fire
        self.publish = publish
        self._lock = threading.Lock()
        self.items: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jeeves-timers")
        self._thread.start()

    def add(self, seconds: float, label: str = "", agent: str | None = None, request: str = "") -> str:
        return self._add(time.time() + seconds, seconds, label, agent, request)

    def add_at(self, when: _dt.datetime, label: str = "", agent: str | None = None, request: str = "") -> str:
        end = when.timestamp()
        return self._add(end, end - time.time(), label, agent, request)

    def _add(self, end: float, total: float, label: str, agent: str | None, request: str) -> str:
        tid = uuid.uuid4().hex[:8]
        with self._lock:
            self.items[tid] = {"id": tid, "end": end, "total": total, "label": label, "agent": agent,
                               "request": request, "kind": "schedule" if request else "timer"}
        self._broadcast()
        return tid

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            return [dict(t, remaining=max(0.0, t["end"] - now)) for t in sorted(self.items.values(),
                                                                               key=lambda t: t["end"])]

    def cancel(self, label_or_id: str | None = None) -> int:
        with self._lock:
            if label_or_id is None:
                n = len(self.items)
                self.items.clear()
            else:
                key = label_or_id.lower()
                drop = [k for k, t in self.items.items() if k == label_or_id or key in (t["label"] or "").lower()]
                for k in drop:
                    del self.items[k]
                n = len(drop)
        self._broadcast()
        return n

    def _broadcast(self) -> None:
        self.publish("timers", self.list())

    def _run(self) -> None:
        while not self._stop.wait(0.25):
            now = time.time()
            due = []
            with self._lock:
                for k, t in list(self.items.items()):
                    if t["end"] <= now:
                        due.append(self.items.pop(k))
            for t in due:
                try:
                    self.on_fire(t)
                except Exception:
                    import logging
                    logging.getLogger("jeeves.timers").exception("timer callback failed")
            if due:
                self._broadcast()

    def stop(self) -> None:
        self._stop.set()
