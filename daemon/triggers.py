"""Event triggers: run a request (or composed steps) when something happens.

Events: app_focused, app_opened, app_closed, process_started, process_stopped,
text_on_screen, time, file_changed, audio_keyword.

Triggers come from three places: ``settings.triggers`` (declarative, can be
set in NixOS), triggers created by the ``event_trigger`` partial (saved in
``~/.local/share/jeeves/triggers.json``), and ``when`` steps inside a running
composition (in memory only).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable

from .. import paths
from ..util import parse_clock
from . import desktop as dk

log = logging.getLogger("jeeves.triggers")
SCREEN_POLL_SECONDS = 10.0


class Triggers:
    def __init__(self, settings: Any, fire: Callable[[dict[str, Any]], None]) -> None:
        self.settings = settings
        self.fire = fire
        self._lock = threading.Lock()
        self.file = paths.data_dir() / "triggers.json"
        self.saved: list[dict[str, Any]] = self._load()
        self.runtime: list[dict[str, Any]] = []
        self._last_focus = ""
        self._last_apps: set[str] = set()
        self._last_procs: set[str] = set()
        self._mtimes: dict[str, float] = {}
        self._last_screen = 0.0
        self._declared_at: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jeeves-triggers")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- storage ---------------------------------------------------------
    def _load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(self.saved, indent=2, default=str))

    def all(self) -> list[dict[str, Any]]:
        declared = [dict(t, id=t.get("id") or f"settings-{i}", declared=True)
                    for i, t in enumerate(self.settings.get("triggers", []) or [])]
        with self._lock:
            return declared + list(self.saved) + [{k: v for k, v in t.items() if k != "steps"} | {"runtime": True}
                                                  for t in self.runtime]

    def _prepare(self, spec: dict[str, Any]) -> dict[str, Any]:
        t = dict(spec)
        t.setdefault("id", uuid.uuid4().hex[:8])
        t.setdefault("repeat", False)
        if t.get("event") == "time" and "at" not in t:
            t["at"] = parse_clock(str(t.get("target", ""))).timestamp()
        if t.get("event") == "file_changed":
            path = os.path.expanduser(str(t.get("target", "")))
            self._mtimes[t["id"]] = _mtime(path)
        return t

    def add(self, spec: dict[str, Any], agent: str | None, request: str) -> str:
        t = self._prepare(dict(spec, agent=agent, request=request))
        with self._lock:
            self.saved.append(t)
            self._save()
        return t["id"]

    def add_steps(self, spec: dict[str, Any], agent: str | None, steps: list, variables: dict) -> str:
        t = self._prepare(dict(spec, agent=agent, steps=steps, variables=variables))
        with self._lock:
            self.runtime.append(t)
        return t["id"]

    def remove(self, trigger_id: str) -> bool:
        with self._lock:
            n = len(self.saved) + len(self.runtime)
            self.saved = [t for t in self.saved if t["id"] != trigger_id]
            self.runtime = [t for t in self.runtime if t["id"] != trigger_id]
            self._save()
            return n != len(self.saved) + len(self.runtime)

    # ---- matching --------------------------------------------------------
    def _active(self) -> list[dict[str, Any]]:
        declared = []
        for i, t in enumerate(self.settings.get("triggers", []) or []):
            t = dict(t)
            t.setdefault("id", f"settings-{i}")
            t["repeat"] = True    # declarative triggers stay
            if t.get("event") == "time" and "at" not in t:
                if t["id"] not in self._declared_at:
                    try:
                        self._declared_at[t["id"]] = parse_clock(str(t.get("target", ""))).timestamp()
                    except ValueError:
                        continue
                t["at"] = self._declared_at[t["id"]]
            declared.append(t)
        with self._lock:
            return declared + list(self.saved) + list(self.runtime)

    def _fire(self, t: dict[str, Any], detail: str) -> None:
        log.info("trigger %s fired (%s)", t.get("id"), detail)
        if not t.get("repeat"):
            self.remove(t["id"])
        elif t.get("event") == "time":
            t["at"] = t["at"] + 86400   # daily
            if t["id"] in self._declared_at:
                self._declared_at[t["id"]] = t["at"]
        try:
            self.fire(dict(t, detail=detail))
        except Exception:
            log.exception("trigger action failed")

    def on_transcript(self, text: str, source: str) -> None:
        low = text.lower()
        for t in self._active():
            if t.get("event") != "audio_keyword":
                continue
            want = t.get("source", "microphone")
            if want != "both" and want != source:
                continue
            if str(t.get("target", "")).lower() in low:
                self._fire(t, f"heard '{t['target']}' ({source})")

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._tick()
            except Exception:
                log.exception("trigger poll failed")

    def _tick(self) -> None:
        active = self._active()
        if not active:
            return
        events = {t.get("event") for t in active}
        now = time.time()
        if events & {"app_focused"}:
            try:
                w = dk.focused()
            except dk.DesktopUnavailable:
                w = None
            app = w.app if w else ""
            if app != self._last_focus:
                self._last_focus = app
                for t in active:
                    if t.get("event") == "app_focused" and dk.app_matches([str(t.get("target", ""))],
                                                                         [app, w.title if w else ""]):
                        self._fire(t, f"{app} focused")
        if events & {"app_opened", "app_closed"}:
            apps = set(dk.open_apps())
            opened, closed = apps - self._last_apps, self._last_apps - apps
            first = not self._last_apps
            self._last_apps = apps
            if not first:
                for t in active:
                    if t.get("event") == "app_opened" and dk.app_matches([str(t.get("target"))], list(opened)):
                        self._fire(t, "app opened")
                    if t.get("event") == "app_closed" and dk.app_matches([str(t.get("target"))], list(closed)):
                        self._fire(t, "app closed")
        if events & {"process_started", "process_stopped"}:
            procs = set(dk.processes())
            started, stopped = procs - self._last_procs, self._last_procs - procs
            first = not self._last_procs
            self._last_procs = procs
            if not first:
                for t in active:
                    target = str(t.get("target", "")).lower()
                    if t.get("event") == "process_started" and target in started:
                        self._fire(t, f"{target} started")
                    if t.get("event") == "process_stopped" and target in stopped:
                        self._fire(t, f"{target} stopped")
        for t in active:
            if t.get("event") == "time" and t.get("at") and now >= float(t["at"]):
                self._fire(t, "time reached")
            elif t.get("event") == "file_changed":
                path = os.path.expanduser(str(t.get("target", "")))
                m = _mtime(path)
                prev = self._mtimes.setdefault(t["id"], m)
                if m != prev:
                    self._mtimes[t["id"]] = m
                    self._fire(t, f"{path} changed")
        if "text_on_screen" in events and now - self._last_screen >= SCREEN_POLL_SECONDS:
            self._last_screen = now
            from ..functions.partials.screen import ocr_words, screenshot
            try:
                shot = screenshot()
            except Exception:
                return
            try:
                text = " ".join(w["text"] for w in ocr_words(shot)).lower()
            except Exception:
                text = ""
            finally:
                shot.unlink(missing_ok=True)
            for t in active:
                if t.get("event") == "text_on_screen" and str(t.get("target", "")).lower() in text:
                    self._fire(t, f"'{t['target']}' appeared on screen")


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return -1.0
