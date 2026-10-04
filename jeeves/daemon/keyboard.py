"""Keybinds (Text Request, Voice Request, Manual Response Review, Abort).

Watches every real keyboard read-only through evdev -- the same way Puppetry
does: devices are never grabbed, so typing is unaffected. Jeeves' own virtual
devices are ignored. Without input-group access this is off and the CLI
commands (bind them in the compositor) do the same thing:

    jeeves --manual_request=text
    jeeves --manual_request=voice --agent=jeeves
    jeeves --review
    jeeves --abort
"""
from __future__ import annotations

import logging
import selectors
import threading
import time
from typing import Any, Callable

log = logging.getLogger("jeeves.keyboard")

try:
    import evdev
    from evdev import ecodes as e
except Exception:  # pragma: no cover
    evdev = None  # type: ignore
    e = None  # type: ignore


class Keyboard:
    def __init__(self, settings: Any, on_bind: Callable[[str, dict[str, Any]], None]) -> None:
        self.settings = settings
        self.on_bind = on_bind
        self._held: set[str] = set()
        self._fired: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "off"

    def start(self) -> None:
        if evdev is None:
            self.status = "python-evdev isn't installed"
            return
        if not self.settings.get("general.watch_keyboard_for_keybinds", True):
            self.status = "disabled in settings"
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="jeeves-keyboard")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def held(self) -> list[str]:
        return sorted(self._held)

    def _bindings(self) -> list[tuple[str, dict[str, Any], set[str]]]:
        s = self.settings
        out: list[tuple[str, dict[str, Any], set[str]]] = []
        abort = s.get("general.abort_key")
        if abort:
            out.append(("abort", {}, {abort} if isinstance(abort, str) else set(abort)))
        mr = s.get("manual_request", {})
        if mr.get("text_keybind_enabled") and mr.get("text_keybind"):
            out.append(("text_request", {}, set(mr["text_keybind"])))
        if mr.get("voice_keybind_enabled"):
            for agent, combo in (mr.get("voice_keybinds") or {}).items():
                if combo:
                    out.append(("voice_request", {"agent": agent}, set(combo)))
        if mr.get("toggle_keybind"):
            out.append(("toggle", {}, set(mr["toggle_keybind"])))
        if mr.get("review_keybind"):
            out.append(("review", {}, set(mr["review_keybind"])))
        return out

    def _devices(self) -> list[Any]:
        found = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except OSError:
                continue
            if d.name.startswith("jeeves-") or "macro-daemon" in d.name:
                d.close()
                continue
            caps = d.capabilities().get(e.EV_KEY, [])
            if e.KEY_A in caps and e.KEY_SPACE in caps:
                found.append(d)
            else:
                d.close()
        return found

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                devices = self._devices()
            except OSError as exc:
                self.status = f"can't list input devices: {exc}"
                self._stop.wait(5)
                continue
            if not devices:
                self.status = "no readable keyboard (is the user in the 'input' group?)"
                self._stop.wait(5)
                continue
            self.status = f"watching {len(devices)} keyboard(s)"
            sel = selectors.DefaultSelector()
            for d in devices:
                sel.register(d, selectors.EVENT_READ)
            known = set(evdev.list_devices())
            last_scan = time.time()
            try:
                while not self._stop.is_set() and sel.get_map():
                    for key, _ in sel.select(timeout=1.0):
                        dev = key.fileobj
                        try:
                            for ev in dev.read():
                                if ev.type == e.EV_KEY:
                                    self._handle(ev.code, ev.value)
                        except OSError:   # unplugged
                            sel.unregister(dev)
                    if time.time() - last_scan > 10:
                        last_scan = time.time()
                        if set(evdev.list_devices()) != known:
                            break         # something was plugged in: rescan
            finally:
                for d in devices:
                    try:
                        d.close()
                    except OSError:
                        pass
                sel.close()

    def _handle(self, code: int, value: int) -> None:
        names = e.KEY.get(code) or e.BTN.get(code)
        if names is None:
            return
        name = names[0] if isinstance(names, list) else names
        if value == 1:
            self._held.add(name)
        elif value == 0:
            self._held.discard(name)
            # a combo can fire again once any of its keys is released
            self._fired = {b for b in self._fired if name not in b.split("+")}
            return
        else:
            return  # autorepeat
        for action, params, combo in self._bindings():
            tag = "+".join(sorted(combo))
            if combo and combo <= self._held and name in combo and tag not in self._fired:
                self._fired.add(tag)
                try:
                    self.on_bind(action, params)
                except Exception:
                    log.exception("keybind handler failed")
