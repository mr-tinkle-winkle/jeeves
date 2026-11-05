"""Rudimentary agents (Agents > Abilities > Rudimentary agent): an agent with exactly one function.

It doesn't have to work out WHICH function you want, only HOW you want that one done, so it's fast:

  - a function with nothing to fill in (say, "Pause music") runs as soon as its name is heard -- no
    model at all;
  - one with arguments (Timers: how long, what for) has them filled in by the intent model from a
    prompt about that one function alone -- a few lines instead of the whole Dictionary -- without
    thinking, and with a short answer; unmistakable requests skip even that (the same rules and
    keyword matching full agents use);
  - if something it needs is missing ("Timer" with no length) it asks for just that.

It never falls back to another function (no chat replies, no research): anything it's told is taken
as a way of running its one function.

The function can be any full function (built-in or your own), one of the agent's own Commands, or
"Run commands" (below): what you say after its name is the command line itself.

"Mute" keeps a rudimentary agent silent: no wake-up reply, no spoken answers (text still shows)."""
from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .base import Arg, FunctionDef, FunctionError

ARGS_SYSTEM = """You fill in the arguments of the one function {agent} (a voice assistant) runs. Every request is for
this function; work out how the user wants it done. For an argument with listed values, use one of them;
leave out arguments the request doesn't need. Only if it can't run without a missing detail, reply
{{"question": "<ask for just that detail>"}}.
Reply with JSON only: {{"args": {{...}}}}

{dictionary}
"""


def function_name(agent: dict[str, Any] | None) -> str | None:
    """The agent's one function, when it's a rudimentary agent."""
    r = (agent or {}).get("rudimentary") or {}
    if isinstance(r, dict) and r.get("enabled") and r.get("function"):
        return str(r["function"])
    return None


NAME = "run_what_i_say"
RISKY = {"rm", "rmdir", "dd", "mkfs", "shred", "wipefs", "fdisk", "parted", "reboot", "poweroff", "shutdown", "halt",
         "kill", "pkill", "killall", "sudo", "doas", "pkexec", "su", "chmod", "chown", "mv", "truncate", "format"}
SPOKEN = {"dash": "-", "hyphen": "-", "minus": "-", "dot": ".", "period": ".", "slash": "/", "underscore": "_",
          "tilde": "~", "colon": ":", "equals": "=", "plus": "+", "at": "@", "comma": ","}
JOINING = {"-", ".", "/", "_", ":", "=", "+", "@", ","}


def settings_of(agent: dict[str, Any] | None) -> dict[str, Any] | None:
    r = (agent or {}).get("rudimentary") or {}
    return r if isinstance(r, dict) and r.get("enabled") else None


# ---------------------------------------------------------------------------- "Run commands"
# One of the functions a rudimentary agent can have: whatever you say after its name is run as a
# command ("Box, firefox"; "Box, systemctl dash dash user restart pipewire"), or -- given a program --
# what you say becomes that program's arguments ("Media, next" -> playerctl next). Spoken words are
# tidied (dash -> -, dot -> ., slash -> /, tilde -> ~); typed requests run exactly as typed. No shell;
# asks first for risky programs (rm, dd, kill, reboot, sudo ...) unless set to never or always.


def function_def(agent: dict[str, Any]) -> FunctionDef:
    r = settings_of(agent) or {}
    prog = (r.get("program") or "").strip()
    desc = (f"Runs {prog} with whatever you say as its arguments." if prog else
            "Runs whatever you say as a command (program and arguments).")
    return FunctionDef(name=NAME, kind="full", title="Run commands", description=desc,
                       how="No AI: the words after the agent's name are the command line. Runs without a shell.",
                       args=[Arg("command", "string", "what was said", required=False, default="", said=True)],
                       category="commands", source="agent", impl=run)


# ---------------------------------------------------------------------------- words -> argv
def tidy_spoken(text: str) -> list[str]:
    """Spoken words -> command-line words. "dash dash user" -> --user; "dash l" -> -l (a short option);
    "play dash pause" -> play-pause; "example dot com" -> example.com."""
    t = text.strip().strip("\"'“”‘’").strip()
    t = re.sub(r"[.!?,;]+$", "", t).strip()
    words = [w.strip(",;!?\"“”").lower() for w in t.split()]
    words = [w for w in words if w]
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        sym = SPOKEN.get(w)
        if sym is None:
            out.append(w)
            i += 1
            continue
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if sym == "-" and nxt in ("dash", "hyphen", "minus"):         # --option
            opt = words[i + 2] if i + 2 < len(words) else ""
            out.append("--" + opt)
            i += 3
            continue
        if sym == "-" and (not out or len(nxt) <= 2):                  # -l, -la: a short option
            out.append("-" + nxt)
            i += 2
            continue
        starts_path = sym == "~" or (sym == "/" and (not out or not re.search(r"[./~]", out[-1])))
        if starts_path:                                                # ~/nix, /tmp: a new word
            out.append(sym + (nxt if nxt and SPOKEN.get(nxt) is None else ""))
            i += 2 if nxt and SPOKEN.get(nxt) is None else 1
            continue
        if out and nxt and SPOKEN.get(nxt) is None:                    # joins two words: play-pause
            out[-1] += sym + nxt
            i += 2
            continue
        if out:
            out[-1] += sym
        else:
            out.append(sym + nxt)
            i += 1
        i += 1
    return out


def _executables() -> list[str]:
    names: set[str] = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names.update(n for n in os.listdir(d) if os.access(os.path.join(d, n), os.X_OK))
        except OSError:
            continue
    return sorted(names)


def find_program(words: list[str]) -> tuple[str, int]:
    """(the program, how many of the words it took): the word itself, two or three words joined with a
    dash or nothing ("obs studio" -> obs-studio), or the closest installed name for a misheard one."""
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        for joiner in ("-", "", "_"):
            cand = joiner.join(words[:n])
            if shutil.which(cand):
                return cand, n
    first = words[0]
    if os.sep in first and os.path.isfile(os.path.expanduser(first)):
        return os.path.expanduser(first), 1
    close = difflib.get_close_matches(first, _executables(), n=1, cutoff=0.84)
    if close:
        return close[0], 1
    return first, 1


def argv_for(text: str, program: str = "", spoken: bool = True) -> list[str]:
    if spoken:
        words = tidy_spoken(text)
    else:
        try:
            words = shlex.split(text)
        except ValueError as exc:
            raise FunctionError(f"that command can't be read: {exc}") from exc
    if program:
        try:
            base = shlex.split(program)
        except ValueError as exc:
            raise FunctionError(f"the agent's program can't be read: {exc}") from exc
        argv = base + words
    else:
        if not words:
            raise FunctionError("run what? Say the command after my name")
        prog, used = find_program(words) if spoken else (os.path.expanduser(words[0]), 1)
        argv = [prog] + words[used:]
    argv[0] = os.path.expanduser(argv[0])
    return [os.path.expanduser(a) if a.startswith("~/") else a for a in argv]


def risky(argv: list[str]) -> bool:
    prog = os.path.basename(argv[0])
    return prog in RISKY or prog.startswith("mkfs") or \
        (prog in ("systemctl", "loginctl") and any(a in ("poweroff", "reboot", "halt", "suspend", "hibernate")
                                                   for a in argv[1:]))


# ---------------------------------------------------------------------------- running
def run(ctx: Any, command: str = "") -> str:
    r = settings_of(ctx.agent) or {}
    source = str(ctx.entry.get("source", "text")).split(":", 1)[0]
    spoken = source not in ("text", "cli", "dry_run") or bool(r.get("tidy_typed"))
    argv = argv_for(command if command else ctx.entry.get("text", ""), (r.get("program") or "").strip(), spoken)
    if not shutil.which(argv[0]) and not os.path.isfile(argv[0]):
        raise FunctionError(f"'{argv[0]}' isn't installed or isn't on PATH")
    shown = shlex.join(argv)
    ctx.trace("command", argv=argv)
    if ctx.dry_run:
        return f"<run {shown}>"
    ask = r.get("confirm", "risky")
    if ask == "always" or (ask == "risky" and risky(argv)):
        keyword = ctx.settings.get("run_command.confirm_keyword", "proceed")
        if not ctx.confirm(f"Run: {shown}", hint=f"Click the indicator or say '{keyword}'."):
            raise FunctionError(f"didn't run {argv[0]} (not confirmed)")
    if r.get("terminal"):
        from .commands import terminal
        subprocess.Popen(terminal(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return ctx.say(f"Running {os.path.basename(argv[0])} in a terminal.")
    ctx.state("thinking", f"Running {shown}")
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as exc:
            raise FunctionError(f"couldn't start {argv[0]}: {exc.strerror or exc}") from exc
        deadline = time.time() + float(r.get("wait", 2.5))
        while proc.poll() is None and time.time() < deadline:
            ctx.check_cancelled()
            time.sleep(0.05)
        if proc.poll() is None:                    # still going (an app): it's started, leave it be
            return ctx.say(f"Started {os.path.basename(argv[0])}.")
        out.seek(0)
        text = out.read().decode(errors="replace").rstrip()
    if proc.returncode != 0:
        if text:
            ctx.show(f"$ {shown}\n{text[-4000:]}")
        last = next((ln for ln in reversed(text.splitlines()) if ln.strip()), "")
        raise FunctionError(f"{os.path.basename(argv[0])} failed" + (f": {last.strip()[:200]}" if last else
                                                                    f" (exit status {proc.returncode})"))
    if text and len(text) <= 200 and text.count("\n") <= 2:
        return ctx.say(text)                       # short output (date, a status): said
    if text:
        ctx.show(f"$ {shown}\n{text[-4000:]}")
        return ctx.say("Done. The output's on screen.")
    return ctx.say("Done.")
