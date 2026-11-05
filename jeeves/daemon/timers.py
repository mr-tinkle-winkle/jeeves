"""Timers and schedules. Shown by the overlay in the bottom-right corner."""
from __future__ import annotations

import datetime as _dt
import threading
import time
import uuid
from typing import Any, Callable


def tick_pcm(rate: int = 16000) -> bytes:
    """One second of a clock: a short wooden tick, then silence."""
    import math
    import struct
    n = rate
    out = []
    for i in range(n):
        t = i / rate
        v = 0.0
        if t < 0.03:
            v = (0.45 * math.sin(2 * math.pi * 1800 * t) + 0.25 * math.sin(2 * math.pi * 900 * t)) * math.exp(-t * 160)
        out.append(struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000)))
    return b"".join(out)


class Ticker:
    """Plays the tick once a second, landing on the timer's whole seconds, until stopped."""

    def __init__(self, target: str, end: float) -> None:
        self.target, self.end = target, end
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="jeeves-timer-tick")
        self.thread.start()

    def _run(self) -> None:
        import subprocess
        from .audio import _player
        rate = 16000
        sec = tick_pcm(rate)
        try:
            proc = subprocess.Popen(_player(True, rate, self.target), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (RuntimeError, OSError):
            return
        try:
            # start mid-second so the ticks fall on the whole seconds left
            frac = (self.end - time.time()) % 1.0
            lead = int(frac * rate) * 2
            assert proc.stdin is not None
            proc.stdin.write(b"\0" * lead)
            fed = frac
            start = time.monotonic()
            while not self._stop.is_set() and time.time() < self.end - 0.5:
                ahead = fed - (time.monotonic() - start)
                if ahead > 0.3:
                    self._stop.wait(ahead - 0.3)
                    continue
                proc.stdin.write(sec)
                proc.stdin.flush()
                fed += 1.0
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.terminate() if self._stop.is_set() else proc.stdin.close()   # type: ignore[union-attr]
            except OSError:
                pass

    def ticking_for(self, now: float) -> dict[str, Any] | None:
        """The timer whose ticking should be heard now (Listening & Keys > Timers), or None."""
        s = self.settings
        if s is None or not s.get("timers.tick", False):
            return None
        under = float(s.get("timers.tick_under_seconds", 60) or 60)
        only = bool(s.get("timers.tick_only_under", True))
        with self._lock:
            live = [t for t in self.items.values() if t["kind"] == "timer" and t["end"] > now]
        live.sort(key=lambda t: t["end"])
        for t in live:
            if not only or t["end"] - now <= under:
                return t
        return None

    def _tick(self, now: float) -> None:
        t = self.ticking_for(now)
        if t is None:
            if self.ticker is not None:
                self.ticker.stop()
                self.ticker = None
            return
        if self.ticker is None or self.ticker.end != t["end"] or not self.ticker.thread.is_alive():
            if self.ticker is not None:
                self.ticker.stop()
            self.ticker = Ticker(self.settings.get("audio.speaker", "") if self.settings else "", t["end"])

    def stop(self) -> None:
        self._stop.set()
        if self.ticker is not None:
            self.ticker.stop()


class Timers:
    def __init__(self, on_fire: Callable[[dict[str, Any]], None], publish: Callable[[str, Any], None],
                 settings: Any = None) -> None:
        self.on_fire = on_fire
        self.publish = publish
        self.settings = settings
        self.ticker: Ticker | None = None
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
            self._tick(now)
            for t in due:
                try:
                    self.on_fire(t)
                except Exception:
                    import logging
                    logging.getLogger("jeeves.timers").exception("timer callback failed")
            if due:
                self._broadcast()

    def ticking_for(self, now: float) -> dict[str, Any] | None:
        """The timer whose ticking should be heard now (Listening & Keys > Timers), or None."""
        s = self.settings
        if s is None or not s.get("timers.tick", False):
            return None
        under = float(s.get("timers.tick_under_seconds", 60) or 60)
        only = bool(s.get("timers.tick_only_under", True))
        with self._lock:
            live = [t for t in self.items.values() if t["kind"] == "timer" and t["end"] > now]
        live.sort(key=lambda t: t["end"])
        for t in live:
            if not only or t["end"] - now <= under:
                return t
        return None

    def _tick(self, now: float) -> None:
        t = self.ticking_for(now)
        if t is None:
            if self.ticker is not None:
                self.ticker.stop()
                self.ticker = None
            return
        if self.ticker is None or self.ticker.end != t["end"] or not self.ticker.thread.is_alive():
            if self.ticker is not None:
                self.ticker.stop()
            self.ticker = Ticker(self.settings.get("audio.speaker", "") if self.settings else "", t["end"])

    def stop(self) -> None:
        self._stop.set()
        if self.ticker is not None:
            self.ticker.stop()
