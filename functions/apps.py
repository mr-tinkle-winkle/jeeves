"""Apps: starting Steam games, and setups -- every open app where you left it.

Steam: the installed games are read from Steam's own library files (appmanifest_*.acf in each
library folder), so "play Elden Ring" works by name, and the game starts through Steam
(steam://rungameid/<id>).

Setups ("save my setup as streaming", "get set up for streaming"): which apps are open, and each
window's position, size, workspace and state (fullscreen, kept above, minimized...). Getting set
up starts what isn't running (from its desktop entry, else the command line it was started with),
waits for its window and puts every window back."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .. import paths
from ..util import normalize, similarity, which
from .base import Arg, FunctionError, full

# ---------------------------------------------------------------------------------------------- Steam
STEAM_ROOTS = ["~/.local/share/Steam", "~/.steam/steam", "~/.steam/root",
               "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"]
NOT_GAMES = re.compile(r"^(proton|steam linux runtime|steamworks common|steamvr|steam audio|"
                       r"sniper|soldier|scout)\b", re.I)


def _vdf_values(text: str, key: str) -> list[str]:
    return re.findall(rf'"{key}"\s+"([^"]*)"', text, re.I)


def steam_libraries() -> list[Path]:
    libs: list[Path] = []
    for root in STEAM_ROOTS:
        r = Path(os.path.expanduser(root))
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if (r / "steamapps").is_dir():
            libs.append(r / "steamapps")
        if vdf.is_file():
            try:
                for p in _vdf_values(vdf.read_text(errors="replace"), "path"):
                    libs.append(Path(p) / "steamapps")
            except OSError:
                pass
    out: list[Path] = []
    for lib in libs:
        try:
            real = lib.resolve()
        except OSError:
            continue
        if real.is_dir() and real not in out:
            out.append(real)
    return out


def steam_games() -> list[dict[str, str]]:
    """[{appid, name}] of the installed games (tools like Proton left out)."""
    games: dict[str, str] = {}
    for lib in steam_libraries():
        for acf in lib.glob("appmanifest_*.acf"):
            try:
                text = acf.read_text(errors="replace")
            except OSError:
                continue
            appid = (_vdf_values(text, "appid") or [""])[0]
            name = (_vdf_values(text, "name") or [""])[0]
            if appid and name and not NOT_GAMES.match(name):
                games[appid] = name
    return [{"appid": k, "name": v} for k, v in sorted(games.items(), key=lambda kv: kv[1].lower())]


def match_game(said: str, games: list[dict[str, str]]) -> dict[str, str] | None:
    want = re.sub(r"[^a-z0-9 ]", " ", normalize(said)).split()
    if not want:
        return None
    best, best_score = None, 0.0
    for g in games:
        name = re.sub(r"[^a-z0-9 ]", " ", normalize(g["name"])).split()
        joined_w, joined_n = "".join(want), "".join(name)
        score = similarity(" ".join(want), " ".join(name))
        if joined_w and joined_w in joined_n:
            score = max(score, 0.75 + 0.25 * len(joined_w) / max(1, len(joined_n)))
        initials = "".join(w[0] for w in name if w)
        if len(joined_w) >= 2 and (joined_w == initials or initials.startswith(joined_w) and
                                   len(joined_w) >= len(initials) - 1):   # "gta" -> Grand Theft Auto V
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = g, score
    return best if best_score >= 0.6 else None


@full(
    "steam_game",
    "Starts one of your installed Steam games by name, or lists them.",
    args=[Arg("game", "string", "The game, as said", required=False, default=""),
          Arg("action", "string", "play or list", required=False, default="play", choices=["play", "list"])],
    how="Reads Steam's library files to find the installed games and starts the best match through Steam.",
    keywords=["launch", "start the game", "open steam game", "what games do i have", "steam game"],
    examples=["Jeeves, launch Elden Ring.", "Jeeves, start up Hollow Knight.", "Jeeves, what games do I have?"],
    category="apps",
)
def steam_game(ctx, game="", action="play"):
    games = steam_games()
    if not games:
        raise FunctionError("I can't find a Steam library on this computer")
    if action == "list" or not game:
        names = [g["name"] for g in games]
        return ctx.say(f"You have {len(names)} games installed: " + ", ".join(names[:25]) +
                       (" and more." if len(names) > 25 else "."))
    g = match_game(str(game), games)
    if g is None:
        raise FunctionError(f"I can't find {game} among your installed Steam games")
    if ctx.dry_run:
        return f"<launch {g['name']} ({g['appid']})>"
    exe = which("steam")
    cmd = [exe, f"steam://rungameid/{g['appid']}"] if exe else ["xdg-open", f"steam://rungameid/{g['appid']}"]
    if not exe and not which("xdg-open"):
        raise FunctionError("Steam isn't installed")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return ctx.say(f"Starting {g['name']}.")


# --------------------------------------------------------------------------------------------- setups
def setups_file() -> Path:
    return paths.data_dir() / "setups.json"


def load_setups() -> dict[str, Any]:
    try:
        return json.loads(setups_file().read_text())
    except (OSError, ValueError):
        return {}


def save_setups(data: dict[str, Any]) -> None:
    f = setups_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1))


APP_DIRS = ["~/.local/share/applications", "~/.nix-profile/share/applications",
            "/etc/profiles/per-user/{user}/share/applications", "/run/current-system/sw/share/applications",
            "~/.local/share/flatpak/exports/share/applications", "/var/lib/flatpak/exports/share/applications",
            "/usr/share/applications", "/usr/local/share/applications"]


def _desktop_entries() -> list[Path]:
    user = os.environ.get("USER", "")
    dirs = [Path(os.path.expanduser(d.format(user=user))) for d in APP_DIRS]
    for d in os.environ.get("XDG_DATA_DIRS", "").split(":"):
        if d:
            dirs.append(Path(d) / "applications")
    out, seen = [], set()
    for d in dirs:
        if d.is_dir():
            for f in d.glob("*.desktop"):
                if f.name not in seen:
                    seen.add(f.name)
                    out.append(f)
    return out


def desktop_exec(app: str) -> list[str] | None:
    """How to start an app with this window class: its desktop entry's Exec line."""
    a = app.lower()
    best = None
    for f in _desktop_entries():
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        m = re.search(r"^Exec=(.+)$", text, re.M)
        if not m:
            continue
        wm = (re.search(r"^StartupWMClass=(.+)$", text, re.M) or [None, ""])[1].strip().lower()
        stem = f.stem.lower()
        rank = 3 if wm == a else 2 if stem == a else 1 if stem.endswith("." + a) or a in stem.split(".") else 0
        if rank and (best is None or rank > best[0]):
            best = (rank, m.group(1))
    if best is None:
        return None
    try:
        argv = [p for p in shlex.split(best[1]) if not re.fullmatch(r"%[a-zA-Z]", p)]
    except ValueError:
        return None
    return argv or None


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [p.decode(errors="replace") for p in raw.split(b"\0") if p]


def _window_pid(dk: Any, w: Any) -> int:
    if getattr(w, "pid", 0):
        return int(w.pid)
    try:
        return int(dk._kdo("getwindowpid", w.id))
    except Exception:  # noqa: BLE001
        return 0


STATE_KEYS = ("minimized", "fullscreen", "keepAbove", "keepBelow", "noBorder", "skipTaskbar",
              "maximizeHorizontal", "maximizeVertical", "onAllDesktops", "floating", "pinned", "monitor")


def snapshot(dk: Any) -> list[dict[str, Any]]:
    """Every open window: app, title, place, workspace, state, and how to start it."""
    out = []
    for w in dk.windows():
        if not w.app or w.w <= 0 or w.h <= 0 or w.app in ("plasmashell", "jeeves", "org.kde.plasmashell"):
            continue
        info = dk.window_info(w)
        launch = desktop_exec(w.app) or _cmdline(_window_pid(dk, w))
        out.append({"app": w.app, "title": w.title, "x": w.x, "y": w.y, "w": w.w, "h": w.h,
                    "workspace": w.workspace, "focused": bool(w.focused),
                    "state": {k: info[k] for k in STATE_KEYS if k in info}, "launch": launch})
    return out


def _pair(saved: list[dict[str, Any]], current: list[Any]) -> list[tuple[dict[str, Any], Any]]:
    """Saved windows matched to open ones: same app, then the closest title."""
    free = list(current)
    pairs = []
    for s in saved:
        same = [w for w in free if w.app == s["app"]]
        if not same:
            pairs.append((s, None))
            continue
        w = max(same, key=lambda w: similarity(normalize(w.title), normalize(s["title"])))
        free.remove(w)
        pairs.append((s, w))
    return pairs


def restore(ctx: Any, dk: Any, saved: list[dict[str, Any]], wait: float = 25.0) -> tuple[int, list[str]]:
    """(windows placed, apps that couldn't be started)."""
    pairs = _pair(saved, dk.windows())
    failed: list[str] = []
    started: set[str] = set()
    for s, w in pairs:
        if w is None and s["app"] not in started and s.get("launch"):
            argv = s["launch"]
            if which(argv[0]) or os.path.isfile(argv[0]):
                started.add(s["app"])
                ctx.think(f"Starting {s['app']}")
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, cwd=os.path.expanduser("~"))
            else:
                failed.append(s["app"])
    if started:                                       # wait for their windows
        deadline = time.time() + wait
        while time.time() < deadline:
            ctx.check_cancelled()
            have = {w.app for w in dk.windows()}
            if all(a in have for a in started):
                break
            time.sleep(0.5)
        time.sleep(1.0)                               # let them finish opening before moving them
        pairs = _pair(saved, dk.windows())
    placed = 0
    focus = None
    for s, w in pairs:
        if w is None:
            if s["app"] not in failed and s["app"] not in started:
                failed.append(s["app"])
            continue
        dk.place(w, int(s["x"]), int(s["y"]), int(s["w"]), int(s["h"]), str(s.get("workspace") or ""),
                 s.get("state") or {})
        placed += 1
        if s.get("focused"):
            focus = w
    if focus is not None:
        dk.activate(focus)
    return placed, sorted(set(failed) - {s["app"] for s, w in pairs if w is not None})


@full(
    "setups",
    "Saves the open apps and where every window is under a name, or gets you set up: opens what's missing "
    "and puts each window back. Also lists and deletes setups.",
    args=[Arg("action", "string", "save, restore, list or delete", required=False, default="restore",
              choices=["save", "restore", "list", "delete"]),
          Arg("setup", "string", "The setup's name, e.g. 'streaming' (empty: the default one)", required=False,
              default="")],
    how="Window positions, sizes, workspaces and states come from KWin (kdotool, getWindowInfo) or Hyprland. "
        "Missing apps are started from their desktop entry, else the command line they were started with.",
    keywords=["get set up", "save my setup", "save this setup", "restore my setup", "set up my workspace"],
    examples=["Jeeves, save my setup as streaming.", "Jeeves, get set up for streaming.", "Jeeves, get set up.",
              "Jeeves, what setups do I have?"],
    category="apps",
)
def setups(ctx, action="restore", setup=""):
    from ..daemon import desktop as dk
    key = normalize(str(setup or "")).strip() or "default"
    data = load_setups()
    if action == "list":
        if not data:
            return ctx.say("You haven't saved any setups yet.")
        return ctx.say("Your setups: " + ", ".join(sorted(data)) + ".")
    if action == "delete":
        if key not in data:
            raise FunctionError(f"there's no setup called {key}")
        if not ctx.dry_run:
            del data[key]
            save_setups(data)
        return ctx.say(f"Deleted the {key} setup.")
    if action == "save":
        try:
            wins = snapshot(dk)
        except dk.DesktopUnavailable as exc:
            raise FunctionError(f"I can't see the windows here: {exc}") from exc
        if not wins:
            raise FunctionError("there are no windows open to save")
        if ctx.dry_run:
            return f"<save {len(wins)} windows as {key}>"
        data[key] = {"saved": time.time(), "windows": wins}
        save_setups(data)
        apps = sorted({w["app"].split(".")[-1] for w in wins})
        return ctx.say(f"Saved the {key} setup: {len(wins)} windows ({', '.join(apps[:8])}).")
    if key not in data:
        close = [n for n in data if similarity(n, key) >= 0.7]
        if len(close) == 1:
            key = close[0]
        elif key == "default" and len(data) == 1:
            key = next(iter(data))
        else:
            raise FunctionError(f"there's no setup called {key}" + (f" (you have {', '.join(sorted(data))})"
                                                                     if data else ""))
    if ctx.dry_run:
        return f"<get set up: {key}>"
    ctx.state("thinking", f"Getting set up: {key}")
    try:
        placed, failed = restore(ctx, dk, data[key]["windows"])
    except dk.DesktopUnavailable as exc:
        raise FunctionError(f"I can't move windows here: {exc}") from exc
    return ctx.say(f"You're set up for {key}." + (f" I couldn't start {', '.join(failed)}." if failed else ""))
