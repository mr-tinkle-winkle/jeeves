"""Run Command, Run File, Play Sound, Send Notification."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ...util import run, which
from ..base import Arg, FunctionError, partial
from ..safety import UnsafeCommand, is_trusted, split


def _confirm_command(ctx, argv: list[str]) -> None:
    cfg = ctx.settings.get("run_command", {})
    if not cfg.get("confirm", True) or is_trusted(argv, cfg.get("trusted", [])):
        return
    shown = " ".join(argv)
    keyword = cfg.get("confirm_keyword", "proceed")
    if not ctx.confirm(f"Run: {shown}", hint=f"Click the indicator or say '{keyword}'."):
        raise FunctionError(f"didn't run '{shown}' (not confirmed)")


@partial(
    "run_command",
    "Runs a program and returns what it printed (stdout, and stderr if it failed).",
    args=[Arg("command", "command", "The program and its arguments, e.g. 'puppetry --list'. Shell syntax "
              "(; && | $() > < `) is rejected."),
          Arg("timeout", "number", "Seconds before the program is stopped", required=False, default=None)],
    how="Runs without a shell. Unless the command is on the trusted list, Jeeves first shows what it will run "
        "and waits for you to click the indicator or say the confirm keyword.",
    returns="the command's output text",
    keywords=["run", "execute"],
    category="system",
)
def run_command(ctx, command, timeout=None):
    try:
        argv = split(command)
    except UnsafeCommand as exc:
        raise FunctionError(str(exc)) from exc
    if not which(argv[0]) and not os.path.isfile(argv[0]):
        raise FunctionError(f"'{argv[0]}' isn't installed or isn't on PATH")
    _confirm_command(ctx, argv)
    limit = float(timeout or ctx.settings.get("run_command.timeout_seconds", 30))
    ctx.trace("command", argv=argv)
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise FunctionError(f"'{argv[0]}' was still running after {limit:g}s and was stopped") from exc
    text = out.stdout.rstrip()
    if out.returncode != 0:
        err = out.stderr.strip()
        text = (text + "\n" if text else "") + (err or f"(exit status {out.returncode})")
        ctx.trace("command_failed", status=out.returncode)
    return text


@partial(
    "run_file",
    "Runs an executable file (a script or program) by path and returns its output.",
    args=[Arg("path", "path", "The file to run"),
          Arg("arguments", "list", "Arguments to pass", required=False, default=[])],
    how="Same safety rules and confirmation as run_command.",
    returns="the program's output text",
    category="files",
)
def run_file(ctx, path, arguments=None):
    p = Path(os.path.expanduser(str(path)))
    if not p.is_file():
        raise FunctionError(f"no file at {p}")
    if not os.access(p, os.X_OK):
        raise FunctionError(f"{p} isn't executable (chmod +x it first)")
    return run_command(ctx, [str(p), *[str(a) for a in (arguments or [])]])


@partial(
    "play_sound",
    "Plays a sound file, or a short named system sound.",
    args=[Arg("sound", "string", "A file path, or one of the named sounds",
              required=False, default="complete",
              choices=None)],
    how="Uses pw-play (PipeWire), paplay or aplay, whichever is installed. Named sounds come from the "
        "freedesktop sound theme: complete, bell, message, alarm-clock-elapsed, dialog-warning.",
    category="interaction",
)
def play_sound(ctx, sound="complete"):
    from ...daemon.audio import play_file, theme_sound
    path = Path(os.path.expanduser(str(sound)))
    if not path.is_file():
        found = theme_sound(str(sound))
        if not found:
            raise FunctionError(f"no sound file or named sound '{sound}'")
        path = found
    play_file(path, ctx.settings.get("audio.speaker", ""))
    return str(path)


@partial(
    "send_notification",
    "Shows a desktop notification.",
    args=[Arg("title", "string", "Notification title"),
          Arg("body", "string", "Notification text", required=False, default="")],
    how="Uses notify-send (libnotify).",
    category="interaction",
)
def send_notification(ctx, title, body=""):
    ctx.notify(str(title), str(body))
    return "sent"


def notify(title: str, body: str = "", app: str = "Jeeves") -> None:
    if which("notify-send"):
        try:
            run(["notify-send", "-a", app, title, body], timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
