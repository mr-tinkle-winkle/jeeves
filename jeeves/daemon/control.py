"""Control Mode: virtual input devices jeeves-keyboard, jeeves-mouse,
jeeves-mouse-absolute and (optionally) jeeves-controller, through uinput
(python-evdev). Same approach as Puppetry's daemon (see puppetry.py for why
Jeeves has its own devices).

Every key/button that is held is tracked, so Abort (and ``release_all``)
can let go of everything.

Absolute moves: on Hyprland the compositor warps the cursor
(``hyprctl dispatch movecursor``). Elsewhere a separate absolute pointer
device (like a VM's USB tablet: ABS_X/ABS_Y spanning the desktop) moves the
cursor to an exact spot without pointer acceleration getting in the way.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..functions.base import Cancelled, FunctionError
from . import desktop as dk

log = logging.getLogger("jeeves.control")

try:  # optional at import time so the daemon runs (minus Control Mode) without it
    import evdev
    from evdev import AbsInfo, UInput
    from evdev import ecodes as e
except Exception:  # pragma: no cover - depends on the machine
    evdev = None  # type: ignore
    e = None  # type: ignore

SIMPLE = {
    "ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL", "shift": "KEY_LEFTSHIFT", "alt": "KEY_LEFTALT",
    "super": "KEY_LEFTMETA", "meta": "KEY_LEFTMETA", "win": "KEY_LEFTMETA", "windows": "KEY_LEFTMETA",
    "enter": "KEY_ENTER", "return": "KEY_ENTER", "esc": "KEY_ESC", "escape": "KEY_ESC", "space": "KEY_SPACE",
    "tab": "KEY_TAB", "backspace": "KEY_BACKSPACE", "delete": "KEY_DELETE", "del": "KEY_DELETE",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT", "home": "KEY_HOME",
    "end": "KEY_END", "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN", "capslock": "KEY_CAPSLOCK",
    "lmb": "BTN_LEFT", "rmb": "BTN_RIGHT", "mmb": "BTN_MIDDLE", "left click": "BTN_LEFT",
    "right click": "BTN_RIGHT", "middle click": "BTN_MIDDLE", "mouse4": "BTN_SIDE", "mouse5": "BTN_EXTRA",
}
CONTROLLER_BUTTONS = {"BTN_SOUTH", "BTN_EAST", "BTN_NORTH", "BTN_WEST", "BTN_TL", "BTN_TR", "BTN_TL2", "BTN_TR2",
                      "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR", "BTN_A", "BTN_B",
                      "BTN_X", "BTN_Y"}
AXES = {"LX": "ABS_X", "LY": "ABS_Y", "RX": "ABS_RX", "RY": "ABS_RY", "LT": "ABS_Z", "RT": "ABS_RZ",
        "DPAD_X": "ABS_HAT0X", "DPAD_Y": "ABS_HAT0Y"}

# US QWERTY: character -> (key, shift)
_SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
            '_': 'MINUS', '+': 'EQUAL', '{': 'LEFTBRACE', '}': 'RIGHTBRACE', '|': 'BACKSLASH', ':': 'SEMICOLON',
            '"': 'APOSTROPHE', '<': 'COMMA', '>': 'DOT', '?': 'SLASH', '~': 'GRAVE'}
_PLAIN = {'-': 'MINUS', '=': 'EQUAL', '[': 'LEFTBRACE', ']': 'RIGHTBRACE', '\\': 'BACKSLASH', ';': 'SEMICOLON',
          "'": 'APOSTROPHE', ',': 'COMMA', '.': 'DOT', '/': 'SLASH', '`': 'GRAVE', ' ': 'SPACE', '\n': 'ENTER',
          '\t': 'TAB'}


def char_key(ch: str) -> tuple[str, bool] | None:
    if ch.isalpha() and ch.isascii():
        return f"KEY_{ch.upper()}", ch.isupper()
    if ch.isdigit():
        return f"KEY_{ch}", False
    if ch in _PLAIN:
        return f"KEY_{_PLAIN[ch]}", False
    if ch in _SHIFTED:
        return f"KEY_{_SHIFTED[ch]}", True
    return None


def key_name(name: str) -> str:
    n = str(name).strip()
    low = n.lower()
    if low in SIMPLE:
        return SIMPLE[low]
    up = n.upper().replace(" ", "")
    if up.startswith(("KEY_", "BTN_")):
        return up
    if len(up) == 1 or (up.startswith("F") and up[1:].isdigit()):
        return f"KEY_{up}"
    return f"KEY_{up}"


class Control:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._kbd = self._mouse = self._abs = self._pad = None
        self._held: set[str] = set()
        self._axes: dict[str, float] = {}
        self.error: str | None = None
        self._abs_size = (1920, 1080)

    # ---- devices ---------------------------------------------------------
    def available(self) -> bool:
        return evdev is not None

    def _ensure(self) -> None:
        if self._kbd is not None:
            return
        if evdev is None:
            raise FunctionError("Control Mode needs python-evdev (it isn't installed)")
        try:
            keys = [c for n, c in e.ecodes.items() if n.startswith("KEY_") and isinstance(c, int) and c < 0x2ff]
            self._kbd = UInput({e.EV_KEY: sorted(set(keys))}, name="jeeves-keyboard")
            self._mouse = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA],
                                  e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL]}, name="jeeves-mouse")
            w, h = dk.screen_size()
            self._abs_size = (w, h)
            self._abs = UInput({e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
                                e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, w - 1, 0, 0, 0)),
                                           (e.ABS_Y, AbsInfo(0, 0, h - 1, 0, 0, 0))]},
                               name="jeeves-mouse-absolute")
            if self.settings.get("control_mode.virtual_controller", False):
                stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
                trig = AbsInfo(0, 0, 255, 0, 0, 0)
                hat = AbsInfo(0, -1, 1, 0, 0, 0)
                self._pad = UInput({
                    e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST, e.BTN_NORTH, e.BTN_WEST, e.BTN_TL, e.BTN_TR,
                               e.BTN_SELECT, e.BTN_START, e.BTN_MODE, e.BTN_THUMBL, e.BTN_THUMBR],
                    e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick), (e.ABS_RY, stick),
                               (e.ABS_Z, trig), (e.ABS_RZ, trig), (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)],
                }, name="jeeves-controller", vendor=0x045e, product=0x028e, version=0x110)
            time.sleep(0.3)  # let the compositor pick the new devices up
        except (OSError, PermissionError) as exc:
            self.close()
            raise FunctionError(f"can't create virtual devices ({exc}); is the user in the 'input' group and "
                                "the uinput module loaded? (services.jeeves in NixOS does both)") from exc

    def close(self) -> None:
        with self._lock:
            for dev in (self._kbd, self._mouse, self._abs, self._pad):
                try:
                    if dev is not None:
                        dev.close()
                except OSError:
                    pass
            self._kbd = self._mouse = self._abs = self._pad = None

    # ---- primitives ------------------------------------------------------
    def _code(self, name: str) -> int:
        code = e.ecodes.get(name)
        if not isinstance(code, int):
            raise FunctionError(f"unknown key '{name}'")
        return code

    def _device_for(self, name: str):
        if name in CONTROLLER_BUTTONS:
            if self._pad is None:
                raise FunctionError("controller buttons need the virtual controller (Settings > Control Mode)")
            return self._pad
        if name.startswith("BTN_"):
            return self._mouse
        return self._kbd

    def key(self, name: str, state: str = "tap", hold: float = 0.05) -> None:
        name = key_name(name)
        with self._lock:
            self._ensure()
            dev = self._device_for(name)
            code = self._code(name)
            if state in ("down", "tap"):
                dev.write(e.EV_KEY, code, 1)
                dev.syn()
                self._held.add(name)
            if state == "tap":
                time.sleep(hold)
            if state in ("up", "tap"):
                dev.write(e.EV_KEY, code, 0)
                dev.syn()
                self._held.discard(name)

    def move(self, x: float, y: float, absolute: bool = False, duration: float = 0.0, ctx: Any = None) -> None:
        with self._lock:
            self._ensure()
            if absolute:
                if dk.move_cursor_absolute(int(x), int(y)):
                    return
                w, h = self._abs_size
                self._abs.write(e.EV_ABS, e.ABS_X, max(0, min(w - 1, int(x))))
                self._abs.write(e.EV_ABS, e.ABS_Y, max(0, min(h - 1, int(y))))
                self._abs.syn()
                return
            steps = max(1, int(duration / 0.01)) if duration > 0 else 1
            done_x = done_y = 0
            for i in range(1, steps + 1):
                if ctx is not None:
                    ctx.check_cancelled()
                tx, ty = round(x * i / steps), round(y * i / steps)
                self._mouse.write(e.EV_REL, e.REL_X, tx - done_x)
                self._mouse.write(e.EV_REL, e.REL_Y, ty - done_y)
                self._mouse.syn()
                done_x, done_y = tx, ty
                if steps > 1:
                    time.sleep(duration / steps)

    def scroll(self, amount: int) -> None:
        with self._lock:
            self._ensure()
            self._mouse.write(e.EV_REL, e.REL_WHEEL, int(amount))
            self._mouse.syn()

    def type_text(self, text: str, per_letter: float = 0.02, ctx: Any = None) -> None:
        for ch in str(text):
            if ctx is not None:
                ctx.check_cancelled()
            mapped = char_key(ch)
            if mapped is None:
                continue
            k, shift = mapped
            if shift:
                self.key("KEY_LEFTSHIFT", "down")
            self.key(k, "tap", hold=0.01)
            if shift:
                self.key("KEY_LEFTSHIFT", "up")
            time.sleep(per_letter)

    def axis(self, axis: str, value: float) -> None:
        with self._lock:
            self._ensure()
            if self._pad is None:
                raise FunctionError("axes need the virtual controller (Settings > Control Mode)")
            name = AXES.get(axis.upper(), axis.upper())
            code = self._code(name)
            if name in ("ABS_Z", "ABS_RZ"):
                raw = int(max(0.0, min(1.0, value)) * 255)
            elif name.startswith("ABS_HAT"):
                raw = int(max(-1, min(1, round(value))))
            else:
                raw = int(max(-1.0, min(1.0, value)) * 32767)
            self._pad.write(e.EV_ABS, code, raw)
            self._pad.syn()
            self._axes[name] = value

    def held(self) -> list[str]:
        with self._lock:
            return sorted(self._held)

    def release_all(self) -> None:
        """Abort: let go of every key and button, recenter every axis."""
        with self._lock:
            if self._kbd is None:
                return
            for name in list(self._held):
                try:
                    dev = self._device_for(name)
                    dev.write(e.EV_KEY, self._code(name), 0)
                    dev.syn()
                except (FunctionError, OSError):
                    pass
            self._held.clear()
            if self._pad is not None:
                for name in list(self._axes):
                    try:
                        self._pad.write(e.EV_ABS, self._code(name), 0)
                    except (FunctionError, OSError):
                        pass
                self._pad.syn()
                self._axes.clear()

    # ---- action lists (Control Mode function) -----------------------------
    def run_actions(self, actions: list[dict[str, Any]], ctx: Any = None) -> int:
        if not isinstance(actions, list):
            raise FunctionError("actions must be a list")
        n = 0
        for a in actions:
            if ctx is not None:
                ctx.check_cancelled()
            if not isinstance(a, dict):
                raise FunctionError(f"bad action {a!r}")
            do = a.get("do")
            if do == "key":
                self.key(a["key"], a.get("state", "tap"), float(a.get("hold", 0.05)))
            elif do == "button":
                self.key(a.get("button", "BTN_LEFT"), a.get("state", "tap"), float(a.get("hold", 0.05)))
            elif do == "move":
                self.move(float(a.get("x", 0)), float(a.get("y", 0)), bool(a.get("absolute", False)),
                          float(a.get("duration", 0)), ctx)
            elif do == "scroll":
                self.scroll(int(a.get("amount", 0)))
            elif do == "type":
                self.type_text(str(a.get("text", "")), ctx=ctx)
            elif do == "wait":
                secs = float(a.get("seconds", 0))
                if ctx is not None:
                    ctx.wait(secs)
                else:
                    time.sleep(secs)
            elif do == "axis":
                self.axis(str(a.get("axis", "LX")), float(a.get("value", 0)))
            elif do == "release_all":
                self.release_all()
            else:
                raise FunctionError(f"unknown action '{do}'")
            n += 1
        return n


__all__ = ["Control", "Cancelled", "key_name", "char_key"]
