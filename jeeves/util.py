"""Small helpers shared by the daemon, functions and CLI (stdlib only)."""
from __future__ import annotations

import datetime as _dt
import difflib
import os
import re
import shutil
import subprocess
import time
from typing import Any

_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?|ms)?", re.I)
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
            "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half": 0.5}


def parse_duration(value: Any) -> float:
    """'5 minutes' / '1h30m' / '90' / 'half an hour' / 2.5 -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty duration")
    text = text.replace("half an hour", "30 minutes").replace("an hour", "1 hour").replace("a minute", "1 minute")
    for word, num in sorted(_WORDNUM.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(word)}\b", str(num), text)
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", text)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), m.group(3)
        return a * 3600 + b * 60 + int(c) if c else a * 60 + b
    total, found = 0.0, False
    for num, unit in _DUR.findall(text):
        found = True
        n = float(num)
        u = (unit or "s").lower()
        if u.startswith("h"):
            total += n * 3600
        elif u == "ms":
            total += n / 1000
        elif u.startswith("m"):
            total += n * 60
        else:
            total += n
    if not found:
        raise ValueError(f"can't read a duration from {value!r}")
    return total


def parse_clock(value: str, now: _dt.datetime | None = None) -> _dt.datetime:
    """'7:30 pm', '19:30', 'tomorrow 9am', 'in 10 minutes' -> next matching datetime."""
    now = now or _dt.datetime.now()
    text = value.strip().lower()
    if text.startswith("in "):
        return now + _dt.timedelta(seconds=parse_duration(text[3:]))
    day = 0
    if "tomorrow" in text:
        day, text = 1, text.replace("tomorrow", "").strip()
    text = text.replace("at ", "").strip()
    if text in ("noon", "midday"):
        text = "12:00"
    elif text == "midnight":
        text = "0:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", text)
    if not m:
        raise ValueError(f"can't read a time from {value!r}")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + _dt.timedelta(days=day)
    if target <= now and day == 0:
        target += _dt.timedelta(days=1)
    return target


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def best_match(needle: str, options: list[str], cutoff: float = 0.6) -> str | None:
    if not options:
        return None
    lowered = {o.lower(): o for o in options}
    if needle.lower() in lowered:
        return lowered[needle.lower()]
    scored = sorted(((similarity(needle, o), o) for o in options), reverse=True)
    return scored[0][1] if scored[0][0] >= cutoff else None


def which(*names: str) -> str | None:
    for n in names:
        if not n:
            continue
        p = shutil.which(n)
        if p:
            return p
    return None


def run(cmd: list[str], timeout: float = 10, input_text: str | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text, check=check)


def desktop() -> str:
    """'kde' | 'hyprland' | 'other'."""
    sync_graphical_env()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    cur = (os.environ.get("XDG_CURRENT_DESKTOP", "") + os.environ.get("DESKTOP_SESSION", "")).lower()
    if "kde" in cur or "plasma" in cur:
        return "kde"
    if "hyprland" in cur:
        return "hyprland"
    return "other"


def graphical_env() -> dict[str, str]:
    """The current env plus the graphical session's display variables from the
    systemd user manager (the daemon may start before the desktop)."""
    env = dict(os.environ)
    if env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
        return env
    try:
        out = run(["systemctl", "--user", "show-environment"], timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return env
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                 "HYPRLAND_INSTANCE_SIGNATURE", "QT_QPA_PLATFORMTHEME", "DBUS_SESSION_BUS_ADDRESS") \
                and k not in env:
            env[k] = v
    return env


_synced = 0.0


def sync_graphical_env() -> None:
    """Copy the desktop's display variables into this process's environment once the
    desktop is up. The daemon is a user service that often starts before Plasma or
    Hyprland: without this it believed it was on an unknown desktop, and spectacle,
    kdotool and grim ran without WAYLAND_DISPLAY -- so it couldn't read the screen or
    find the mouse."""
    global _synced
    now = time.monotonic()
    if not os.environ.get("INVOCATION_ID"):       # only systemd services miss the desktop's env
        return
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_CURRENT_DESKTOP") or now - _synced < 5:
        return
    _synced = now
    try:
        out = run(["systemctl", "--user", "show-environment"], timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for ln in out.splitlines():
        k, _, v = ln.partition("=")
        if k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
                 "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "HYPRLAND_INSTANCE_SIGNATURE",
                 "DBUS_SESSION_BUS_ADDRESS") and v and not os.environ.get(k):
            os.environ[k] = v


def truncate(text: str, n: int = 400) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"
