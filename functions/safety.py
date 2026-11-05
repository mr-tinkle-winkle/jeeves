"""Run Command safety rules.

* Shell metacharacters are rejected outright -- commands are run with
  ``subprocess`` and an argv list, never through a shell, and anything that
  *looks* like shell syntax (``;  &&  ||  |  $()  ``  >  <  &`` ...) is refused
  so nobody is surprised that it didn't work the way a shell would.
* A command on the trusted list skips confirmation. A trusted entry with no
  arguments trusts every argument list for that program; an entry with
  arguments trusts exactly that argument list.
"""
from __future__ import annotations

import shlex

FORBIDDEN = [";", "&&", "||", "|", "$(", "`", ">", "<", "&", "\n", "\r", "${", "$((", "\\\n"]


class UnsafeCommand(ValueError):
    pass


def check_metacharacters(command: str) -> None:
    for tok in FORBIDDEN:
        if tok in command:
            shown = tok.replace("\n", "\\n").replace("\r", "\\r")
            raise UnsafeCommand(f"shell syntax {shown!r} isn't allowed in commands")


def split(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        argv = [str(a) for a in command]
        for a in argv:
            check_metacharacters(a)
    else:
        check_metacharacters(command)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise UnsafeCommand(f"can't parse command: {exc}") from exc
    if not argv:
        raise UnsafeCommand("empty command")
    return argv


def is_trusted(argv: list[str], trusted: list[str]) -> bool:
    import os
    prog = os.path.basename(argv[0])
    for entry in trusted or []:
        try:
            t = shlex.split(entry)
        except ValueError:
            continue
        if not t or os.path.basename(t[0]) != prog:
            continue
        if len(t) == 1:
            return True          # no arguments listed: all arguments are fine
        if argv[1:len(t)] == t[1:] and len(argv) == len(t):
            return True
    return False
