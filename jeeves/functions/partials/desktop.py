"""Mouse, keys, apps/windows, clipboard and screen marking."""
from __future__ import annotations

import subprocess

from ...daemon import desktop as dk
from ...util import run, which
from ..base import Arg, FunctionError, partial


def _wrap(fn, *a):
    try:
        return fn(*a)
    except dk.DesktopUnavailable as exc:
        raise FunctionError(str(exc)) from exc


def _window(app: str) -> dk.Window:
    w = _wrap(dk.find_window, app)
    if w is None:
        raise FunctionError(f"no open window for '{app}'")
    return w


@partial("get_mouse_position", "Gets the mouse cursor's current position on screen.",
         returns="{x, y} in pixels", category="desktop", dry_run_safe=True)
def get_mouse_position(ctx):
    x, y = _wrap(dk.mouse_position)
    return {"x": x, "y": y}


@partial("get_held_keys", "Lists the keys and mouse buttons currently held down (real keyboard/mouse and "
         "anything Control Mode is holding).", returns="list of key names", category="desktop",
         dry_run_safe=True)
def get_held_keys(ctx):
    held = set(ctx.engine.keyboard.held()) if ctx.engine.keyboard else set()
    held |= set(ctx.engine.control.held())
    return sorted(held)


@partial("get_focused_app", "Gets the app that currently has keyboard focus.",
         returns="{app, title}", category="desktop", dry_run_safe=True)
def get_focused_app(ctx):
    w = _wrap(dk.focused)
    return {"app": w.app, "title": w.title} if w else {"app": "", "title": ""}


@partial("get_open_apps", "Lists the apps that have windows open.", returns="list of app names",
         category="desktop", dry_run_safe=True)
def get_open_apps(ctx):
    return _wrap(dk.open_apps)


@partial("get_app_position", "Gets where an app's window is on screen.",
         args=[Arg("app", "app", "The app")], returns="{x, y} of the window's top-left corner",
         category="desktop", dry_run_safe=True)
def get_app_position(ctx, app):
    w = _window(app)
    return {"x": w.x, "y": w.y}


@partial("get_app_workspace", "Gets which workspace (virtual desktop) an app's window is on.",
         args=[Arg("app", "app", "The app")], returns="workspace number or name", category="desktop",
         dry_run_safe=True)
def get_app_workspace(ctx, app):
    return _window(app).workspace


@partial("get_app_size", "Gets an app window's size.", args=[Arg("app", "app", "The app")],
         returns="{w, h} in pixels", category="desktop", dry_run_safe=True)
def get_app_size(ctx, app):
    w = _window(app)
    return {"w": w.w, "h": w.h}


@partial("get_clipboard", "Gets the current clipboard contents (what was last copied).",
         returns="clipboard text", category="clipboard", dry_run_safe=True)
def get_clipboard(ctx):
    return clipboard_get()


@partial("set_clipboard", "Puts text on the clipboard (as if it were copied).",
         args=[Arg("text", "string", "Text to copy")], category="clipboard")
def set_clipboard(ctx, text):
    clipboard_set(str(text))
    return "copied"


@partial("mark_screen_position", "Draws a circle on the screen around a position, to point something out.",
         args=[Arg("x", "integer", "X in pixels"), Arg("y", "integer", "Y in pixels"),
               Arg("radius", "integer", "Circle radius in pixels", required=False, default=40),
               Arg("seconds", "number", "How long it stays", required=False, default=4)],
         how="Drawn by the on-screen overlay, click-through, above other windows.", category="screen")
def mark_screen_position(ctx, x, y, radius=40, seconds=4):
    ctx.engine.publish("mark", {"x": int(x), "y": int(y), "radius": int(radius), "seconds": float(seconds)})
    return {"x": int(x), "y": int(y)}


# ---------------------------------------------------------------------------
# clipboard helpers (Wayland: wl-clipboard; X11: xclip)
# ---------------------------------------------------------------------------

def clipboard_get() -> str:
    if which("wl-paste"):
        out = run(["wl-paste", "--no-newline"], timeout=3)
        return out.stdout if out.returncode == 0 else ""
    if which("xclip"):
        return run(["xclip", "-selection", "clipboard", "-o"], timeout=3).stdout
    raise FunctionError("no clipboard tool installed (wl-clipboard or xclip)")


def clipboard_set(text: str) -> None:
    if which("wl-copy"):
        # wl-copy forks to serve the selection; don't wait on it
        subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True).communicate(text.encode(), timeout=3)
        return
    if which("xclip"):
        run(["xclip", "-selection", "clipboard"], input_text=text, timeout=3)
        return
    raise FunctionError("no clipboard tool installed (wl-clipboard or xclip)")
