"""The Summary log: a rolling, timestamped transcript of the microphone and
desktop audio (kept distinct), covering the last N minutes (default 60).

Written to ``~/.local/state/jeeves/summary.log`` as it grows and pruned to the
window, so it never holds more than the configured time."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from .. import paths

TAGS = {"microphone": "mic", "desktop": "desktop"}


class SummaryLog:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.lines: deque[tuple[float, str, str]] = deque()
        self.file = paths.state_dir() / "summary.log"
        self._last_prune = 0.0

    def enabled(self) -> bool:
        return bool(self.settings.get("summary.enabled", False))

    def add(self, source: str, text: str) -> None:
        if not text.strip() or not self.enabled():
            return
        if source not in (self.settings.get("summary.sources") or ["microphone", "desktop"]):
            return
        now = time.time()
        with self._lock:
            self.lines.append((now, source, text.strip()))
            self._prune(now)
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file, "a") as f:
                f.write(self._fmt(now, source, text.strip()) + "\n")
            if now - self._last_prune > 60:
                self._rewrite()
                self._last_prune = now

    @staticmethod
    def _fmt(t: float, source: str, text: str) -> str:
        return f"[{time.strftime('%H:%M:%S', time.localtime(t))}] [{TAGS.get(source, source)}] {text}"

    def _prune(self, now: float) -> None:
        window = float(self.settings.get("summary.minutes", 60)) * 60
        while self.lines and self.lines[0][0] < now - window:
            self.lines.popleft()

    def _rewrite(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            for t, s, txt in self.lines:
                f.write(self._fmt(t, s, txt) + "\n")
        tmp.replace(self.file)

    def text(self, minutes: float | None = None) -> str:
        now = time.time()
        cutoff = now - float(minutes if minutes is not None else self.settings.get("summary.minutes", 60)) * 60
        with self._lock:
            self._prune(now)
            return "\n".join(self._fmt(t, s, txt) for t, s, txt in self.lines if t >= cutoff)

    def clear(self) -> None:
        with self._lock:
            self.lines.clear()
            self.file.unlink(missing_ok=True)
