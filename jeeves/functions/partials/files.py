"""Read Specific Files, Create Specific Files (Run Specific Files is in system.py)."""
from __future__ import annotations

import os
from pathlib import Path

from ..base import Arg, FunctionError, partial

MAX_READ = 200_000


def _path(p) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


@partial("read_file", "Reads a text file and returns its contents.",
         args=[Arg("path", "path", "The file"),
               Arg("max_chars", "integer", "Read at most this many characters", required=False, default=20000)],
         returns="the file's text", category="files", dry_run_safe=True)
def read_file(ctx, path, max_chars=20000):
    p = _path(path)
    if not p.is_file():
        raise FunctionError(f"no file at {p}")
    with open(p, "r", errors="replace") as f:
        return f.read(min(int(max_chars), MAX_READ))


@partial("create_file", "Creates (or overwrites, or appends to) a text file.",
         args=[Arg("path", "path", "Where to write"), Arg("content", "string", "The text"),
               Arg("mode", "string", "What to do if it exists", required=False, default="create",
                   choices=["create", "overwrite", "append"])],
         how="'create' refuses to replace an existing file.", returns="the path written", category="files")
def create_file(ctx, path, content, mode="create"):
    p = _path(path)
    if mode == "create" and p.exists():
        raise FunctionError(f"{p} already exists (use mode 'overwrite' or 'append')")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if mode == "append" else "w") as f:
        f.write(str(content))
    return str(p)
