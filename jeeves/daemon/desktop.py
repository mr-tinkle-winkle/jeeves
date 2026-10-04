"""Window/app/cursor queries for KDE Plasma (kdotool) and Hyprland (hyprctl),
with a process-list fallback for "is this app open".

All functions return plain data and never raise for "tool missing" -- they
raise ``DesktopUnavailable`` with a message the agent can say out loud.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from ..util import desktop, run, which


class DesktopUnavailable(RuntimeError):
    pass


@dataclass
class Window:
    id: str
    app: str            # class / app id, lower-case
    title: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    workspace: str = ""
    pid: int = 0
    focused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hypr(*args: str) -> Any:
    if not which("hyprctl"):
        raise DesktopUnavailable("hyprctl isn't installed")
    out = run(["hyprctl", "-j", *args], timeout=3)
    if out.returncode != 0:
        raise DesktopUnavailable(out.stderr.strip() or "hyprctl failed")
    return json.loads(out.stdout or "null")


def _kdo(*args: str) -> str:
    if not which("kdotool"):
        raise DesktopUnavailable("kdotool isn't installed (needed on KDE Plasma)")
    out = run(["kdotool", *args], timeout=4)
    if out.returncode != 0:
        raise DesktopUnavailable(out.stderr.strip() or "kdotool failed")
    return out.stdout.strip()


def windows() -> list[Window]:
    d = desktop()
    if d == "hyprland":
        active = (_hypr("activewindow") or {}).get("address")
        out = []
        for c in _hypr("clients") or []:
            (x, y), (w, h) = c.get("at", (0, 0)), c.get("size", (0, 0))
            out.append(Window(id=c.get("address", ""), app=(c.get("class") or "").lower(), title=c.get("title", ""),
                              x=x, y=y, w=w, h=h, workspace=str((c.get("workspace") or {}).get("name", "")),
                              pid=int(c.get("pid") or 0), focused=c.get("address") == active))
        return out
    if d == "kde" or which("kdotool"):
        try:
            active = _kdo("getactivewindow")
        except DesktopUnavailable:
            active = ""
        ids = [i for i in _kdo("search", "--class", ".").splitlines() if i.strip()]
        out = []
        for wid in ids:
            try:
                cls = _kdo("getwindowclassname", wid)
                title = _kdo("getwindowname", wid)
                geo = _kdo("getwindowgeometry", wid)
            except DesktopUnavailable:
                continue
            pos = re.search(r"Position:\s*(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", geo)
            size = re.search(r"Geometry:\s*(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", geo)
            ws = ""
            try:
                ws = _kdo("get_desktop_for_window", wid)
            except DesktopUnavailable:
                pass
            out.append(Window(id=wid, app=cls.lower(), title=title,
                              x=int(float(pos.group(1))) if pos else 0, y=int(float(pos.group(2))) if pos else 0,
                              w=int(float(size.group(1))) if size else 0, h=int(float(size.group(2))) if size else 0,
                              workspace=ws, focused=wid == active))
        return out
    raise DesktopUnavailable("window queries need KDE Plasma (kdotool) or Hyprland")


def focused() -> Window | None:
    d = desktop()
    if d == "hyprland":
        c = _hypr("activewindow") or {}
        if not c:
            return None
        (x, y), (w, h) = c.get("at", (0, 0)), c.get("size", (0, 0))
        return Window(id=c.get("address", ""), app=(c.get("class") or "").lower(), title=c.get("title", ""),
                      x=x, y=y, w=w, h=h, workspace=str((c.get("workspace") or {}).get("name", "")),
                      pid=int(c.get("pid") or 0), focused=True)
    return next((w for w in windows() if w.focused), None)


def processes() -> list[str]:
    """Running process names (comm + argv0 basename), lower-case."""
    names: set[str] = set()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                names.add(f.read().strip().lower())
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "replace")
            if argv0:
                names.add(os.path.basename(argv0).lower())
        except OSError:
            continue
    return sorted(n for n in names if n)


def open_apps() -> list[str]:
    try:
        return sorted({w.app for w in windows() if w.app})
    except DesktopUnavailable:
        return []


def find_window(app: str) -> Window | None:
    app_l = app.lower().strip()
    ws = windows()
    for w in ws:
        if w.app == app_l:
            return w
    for w in ws:
        if app_l in w.app or app_l in w.title.lower():
            return w
    from ..util import best_match
    match = best_match(app_l, [w.app for w in ws], cutoff=0.7)
    return next((w for w in ws if w.app == match), None) if match else None


def app_matches(patterns: list[str], names: list[str]) -> bool:
    """True if any pattern is a substring of any name (case-insensitive)."""
    lowered = [n.lower() for n in names]
    return any(p.lower().strip() and any(p.lower().strip() in n for n in lowered) for p in patterns)


def is_open(patterns: list[str]) -> bool:
    if not patterns:
        return False
    names = open_apps() + processes()
    return app_matches(patterns, names)


def mouse_position() -> tuple[int, int]:
    d = desktop()
    if d == "hyprland":
        pos = _hypr("cursorpos")
        return int(pos["x"]), int(pos["y"])
    out = _kdo("getmouselocation")
    m = re.search(r"x:(-?\d+)\s+y:(-?\d+)", out)
    if not m:
        raise DesktopUnavailable(f"unexpected kdotool output: {out!r}")
    return int(m.group(1)), int(m.group(2))


def move_cursor_absolute(x: int, y: int) -> bool:
    """Exact positioning when the compositor allows it; False otherwise
    (callers then fall back to corner-anchored relative moves)."""
    if desktop() == "hyprland" and which("hyprctl"):
        return run(["hyprctl", "dispatch", "movecursor", str(x), str(y)], timeout=3).returncode == 0
    return False


def screen_size() -> tuple[int, int]:
    if desktop() == "hyprland":
        try:
            mons = _hypr("monitors")
            m = next((m for m in mons if m.get("focused")), mons[0])
            return int(m["width"] / m.get("scale", 1)), int(m["height"] / m.get("scale", 1))
        except (DesktopUnavailable, IndexError, KeyError):
            pass
    if which("kscreen-doctor"):
        try:
            out = run(["kscreen-doctor", "-j"], timeout=4).stdout
            for o in json.loads(out).get("outputs", []):
                if o.get("enabled"):
                    mode = next((m for m in o.get("modes", []) if m.get("id") == o.get("currentModeId")), None)
                    if mode:
                        scale = o.get("scale", 1) or 1
                        return int(mode["size"]["width"] / scale), int(mode["size"]["height"] / scale)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return 1920, 1080
