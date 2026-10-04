"""``jeeves doctor``: checks that Screen Reading and Control Mode can work on this
desktop, and says what to fix when they can't. Runs inside the daemon so it sees
exactly what the daemon sees (environment, permissions, tools)."""
from __future__ import annotations

import os
import time
from typing import Any

from ..util import desktop, which
from . import desktop as dk


def _check(name: str, ok: bool, detail: str, fix: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


def run(control: Any = None, move_test: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = desktop()
    disp = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY") or ""
    out.append(_check("Desktop", bool(disp),
                      f"{d} (display: {disp or 'none'}, XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '')})",
                      "" if disp else "the daemon can't see the desktop session; run "
                      "'systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP' from the desktop, "
                      "or restart the service after logging in"))

    box = None
    known = False
    try:
        outs = dk.outputs()
        box = dk.desktop_box()
        desc = ", ".join(f"{o.name} {o.w}x{o.h}+{o.x}+{o.y}@{o.scale:g}{' (primary)' if o.primary else ''}"
                         for o in outs)
        known = not (len(outs) == 1 and outs[0].name == "default")
        out.append(_check("Monitors", known, f"{desc}; desktop {box[2]}x{box[3]} at {box[0]},{box[1]}",
                          "" if known else "couldn't read the monitor layout (kscreen-doctor or hyprctl); "
                          "clicks may land in the wrong place on multi-monitor or scaled setups"))
    except Exception as exc:
        out.append(_check("Monitors", False, str(exc)))

    from ..functions.partials import screen
    shot = None
    try:
        shot = screen.screenshot()
        iw, ih = screen._image_size(shot)
        mp = screen.Mapper((iw, ih), box if known else None)
        out.append(_check("Screenshot", True, f"{screen.last_tool}: {iw}x{ih} px "
                          f"(1 px = {mp.sx:.2f} x {mp.sy:.2f} desktop units)"))
    except Exception as exc:
        out.append(_check("Screenshot", False, str(exc),
                          "on KDE install spectacle (or allow Jeeves when the desktop asks to share the "
                          "screen); on Hyprland/sway install grim"))
    if shot is not None:
        try:
            words = screen.ocr_words(shot)
            out.append(_check("Reading text (OCR)", bool(words), f"tesseract read {len(words)} words",
                              "" if words else "nothing readable on screen right now, or the screenshot was black"))
        except Exception as exc:
            out.append(_check("Reading text (OCR)", False, str(exc), "install tesseract"))
        finally:
            shot.unlink(missing_ok=True)

    pos = None
    try:
        pos = dk.mouse_position()
        out.append(_check("Mouse position", True, f"{pos[0]},{pos[1]}"))
    except Exception as exc:
        out.append(_check("Mouse position", False, str(exc),
                          "install kdotool (KDE) -- without it moves are relative and less precise"))
    try:
        f = dk.focused()
        out.append(_check("Window queries", True, f"focused: {f.app} -- {f.title}" if f else "no focused window"))
    except Exception as exc:
        out.append(_check("Window queries", False, str(exc)))

    if control is None:
        return out
    if not control.available():
        out.append(_check("Virtual input devices", False, "python-evdev isn't installed"))
        return out
    try:
        with control._lock:
            control._ensure()
        out.append(_check("Virtual input devices", True, "jeeves-keyboard, jeeves-mouse, jeeves-mouse-absolute"))
    except Exception as exc:
        out.append(_check("Virtual input devices", False, str(exc),
                          "enable services.jeeves (adds the uinput rule and the input group), then log out and in"))
        return out
    if move_test and pos is not None and box is not None:
        tx, ty = box[0] + box[2] // 3, box[1] + box[3] // 3
        try:
            control.move(tx, ty, absolute=True)
            time.sleep(0.25)
            got = dk.mouse_position()
            err = abs(got[0] - tx) + abs(got[1] - ty)
            out.append(_check("Exact mouse moves", err <= 4,
                              f"asked for {tx},{ty}, cursor went to {got[0]},{got[1]}",
                              "" if err <= 4 else "the absolute pointer isn't mapped to the whole desktop; "
                              "please report this output"))
        except Exception as exc:
            out.append(_check("Exact mouse moves", False, str(exc)))
        finally:
            try:
                control.move(pos[0], pos[1], absolute=True)
            except Exception:
                pass
    elif move_test:
        out.append(_check("Exact mouse moves", False, "skipped: the mouse position can't be read",
                          "install kdotool (KDE)" if d == "kde" else ""))
    return out


def format_report(checks: list[dict[str, Any]]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{'ok' if c['ok'] else '!!'}] {c['name']}: {c['detail']}")
        if not c["ok"] and c.get("fix"):
            lines.append(f"     -> {c['fix']}")
    return "\n".join(lines)


__all__ = ["run", "format_report", "which"]
