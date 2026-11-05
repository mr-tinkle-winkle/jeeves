"""An agent's own commands (Agents > Commands): programs you give it by name, with arguments you
describe, that it runs with whatever arguments fit what you asked.

    rebuild           nixos-rebuild switch --flake /home/me/nix#{host}      host: which machine (desk, laptop)
    push-nix-config   nix-push {message}                                    message: the commit message

Each becomes a function in that agent's Dictionary (named after the command), so the intent model
picks it like any other and fills in the arguments from your description of them. The command is
run without a shell: {placeholders} are replaced inside single arguments, so whatever is said can't
turn into extra commands. Commands that need a password (sudo) or that you want to watch can run in
a terminal window instead."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from typing import Any

from .base import Arg, FunctionDef, FunctionError

PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w]*)\}")
TERMINALS = [("konsole", ["--hold", "-e"]), ("kitty", ["--hold"]), ("alacritty", ["--hold", "-e"]),
             ("foot", ["--hold"]), ("wezterm", ["start", "--"]), ("ghostty", ["-e"]),
             ("gnome-terminal", ["--"]), ("xterm", ["-hold", "-e"])]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:40] or "command"


def function_name(name: str, taken: set[str]) -> str:
    n = slug(name)
    return n if n not in taken else f"my_{n}"


def placeholders(template: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER.findall(template or "")))


def argv_for(template: str, values: dict[str, Any]) -> list[str]:
    """The program and its arguments, with the {placeholders} filled in. An argument that was only a
    placeholder with nothing to put in it is left out (an optional argument not given)."""
    try:
        parts = shlex.split(template)
    except ValueError as exc:
        raise FunctionError(f"the command line can't be read: {exc}") from exc
    if not parts:
        raise FunctionError("the command is empty")
    out = []
    for p in parts:
        names = PLACEHOLDER.findall(p)
        if names and PLACEHOLDER.fullmatch(p) and values.get(names[0]) in (None, ""):
            continue
        out.append(PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "") if values.get(m.group(1))
                                                 is not None else ""), p))
    out[0] = os.path.expanduser(out[0])
    return [os.path.expanduser(a) if a.startswith("~/") else a for a in out]


def terminal(argv: list[str]) -> list[str]:
    for exe, flags in TERMINALS:
        if shutil.which(exe):
            return [exe, *flags, *argv]
    raise FunctionError("no terminal program found (konsole, kitty, alacritty, foot...) to run it in")


def command_defs(agent: dict[str, Any], taken: set[str]) -> list[FunctionDef]:
    """The agent's commands as functions."""
    out: list[FunctionDef] = []
    used = set(taken)
    for spec in agent.get("commands") or []:
        if not isinstance(spec, dict) or not spec.get("name") or not spec.get("command"):
            continue
        name = function_name(spec["name"], used)
        used.add(name)
        described = {a.get("name"): a for a in spec.get("args") or [] if isinstance(a, dict) and a.get("name")}
        args = []
        for p in placeholders(spec["command"]):
            a = described.get(p, {})
            choices = [c for c in (a.get("choices") or []) if str(c).strip()] or None
            args.append(Arg(p, "string", a.get("description") or p.replace("_", " "),
                            required=bool(a.get("required", False)), default=a.get("default") or "",
                            choices=choices))
        desc = (spec.get("description") or f"Runs {spec['name']}").strip()
        out.append(FunctionDef(
            name=name, kind="full", title=spec["name"], description=desc + f" (your command '{spec['name']}')",
            how=f"Runs: {spec['command']}" + (" in a terminal window" if spec.get("terminal") else ""),
            args=args, keywords=[spec["name"].lower(), spec["name"].lower().replace("-", " ")],
            examples=[str(e) for e in spec.get("examples") or []], category="commands",
            source="agent", impl=_runner(spec)))
    return out


def _runner(spec: dict[str, Any]):
    def run(ctx, **values: Any) -> str:
        return run_spec(ctx, spec, values)
    return run


def run_spec(ctx: Any, spec: dict[str, Any], values: dict[str, Any]) -> str:
    argv = argv_for(spec["command"], values)
    if not shutil.which(argv[0]) and not os.path.isfile(argv[0]):
        raise FunctionError(f"'{argv[0]}' isn't installed or isn't on PATH")
    shown = shlex.join(argv)
    if ctx.dry_run:
        return f"<run {shown}>"
    if spec.get("confirm", True):
        keyword = ctx.settings.get("run_command.confirm_keyword", "proceed")
        if not ctx.confirm(f"Run {spec['name']}: {shown}", hint=f"Click the indicator or say '{keyword}'."):
            raise FunctionError(f"didn't run {spec['name']} (not confirmed)")
    ctx.trace("command", argv=argv)
    if spec.get("terminal"):
        subprocess.Popen(terminal(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return ctx.say(f"Running {spec['name']} in a terminal.")
    ctx.state("thinking", f"Running {spec['name']}")
    limit = float(spec.get("timeout") or 600)
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise FunctionError(f"{spec['name']} was still running after {limit:g} seconds and was stopped") from exc
    text = (out.stdout or "").rstrip()
    err = (out.stderr or "").rstrip()
    full = "\n".join(t for t in (text, err) if t)
    if full:
        ctx.show(full[-4000:])
    if out.returncode != 0:
        last = next((ln for ln in reversed(err.splitlines() or text.splitlines()) if ln.strip()), "")
        raise FunctionError(f"{spec['name']} failed" + (f": {last.strip()[:200]}" if last else
                                                         f" (exit status {out.returncode})"))
    last = next((ln for ln in reversed(text.splitlines()) if ln.strip()), "")
    return ctx.say(f"{spec['name']} finished." + (f" {last.strip()[:160]}" if last and len(last) < 160 else ""))
