"""Functions added by other apps.

An app registers functions by either
  * dropping a manifest into ``~/.local/share/jeeves/imports/<app>.json``, or
  * calling ``jeeves import-functions manifest.json`` (or the ``functions.import``
    socket method).

Manifest::

    {"jeeves_manifest": 1, "app": "afterglow",
     "functions": [{"name": "afterglow_clip", "kind": "full",
                    "description": "Saves the last 30 seconds as a clip.",
                    "keywords": ["clip that"], "args": [...],
                    "steps": [{"call": "run_command", "args": {"command": "afterglow clip"}}]}]}

Imported functions are compositions of Jeeves' partials -- apps can't ship
Python into the daemon. Nothing is installed until the user approves it in
the popup, which lists each function and every default function it uses.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from .. import paths
from ..functions.base import FunctionDef
from ..functions.composer import partials_used, validate

APP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class Imports:
    def __init__(self, registry: Any, publish: Any) -> None:
        self.registry = registry
        self.publish = publish
        self.pending: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, float] = {}

    def submit(self, manifest: dict[str, Any], origin: str = "socket") -> dict[str, Any]:
        app = str(manifest.get("app", "")).strip()
        if not APP_NAME.match(app):
            raise ValueError("manifest needs an 'app' name (letters, digits, . _ -)")
        funcs = manifest.get("functions")
        if not isinstance(funcs, list) or not funcs:
            raise ValueError("manifest has no functions")
        known = set(self.registry.functions)
        items = []
        for raw in funcs:
            f = FunctionDef.from_dict(raw, source=f"app:{app}")
            if f.steps is None:
                raise ValueError(f"'{f.name}' has no steps (imported functions must be compositions)")
            if not re.match(r"^[a-z0-9_]{1,64}$", f.name):
                raise ValueError(f"bad function name '{f.name}'")
            problems = validate(f.steps, known | {x.get("name") for x in funcs})
            existing = self.registry.get(f.name)
            replaces = existing is not None and existing.source == f"app:{app}"
            if existing is not None and not replaces:
                problems.append(f"name clashes with existing {existing.source} function")
            uses = sorted(partials_used(f.steps))
            defaults = [u for u in uses if (self.registry.get(u) and self.registry.get(u).source == "builtin")]
            items.append({"function": f.to_dict(), "uses": uses, "default_functions_used": defaults,
                          "problems": problems, "replaces": replaces})
        pid = uuid.uuid4().hex[:8]
        self.pending[pid] = {"id": pid, "app": app, "origin": origin, "time": time.time(), "items": items}
        self.publish("import_request", self.pending[pid])
        return self.pending[pid]

    def decide(self, pid: str, approve: list[str] | None, deny_all: bool = False) -> dict[str, Any]:
        req = self.pending.pop(pid, None)
        if req is None:
            raise KeyError(pid)
        installed = []
        if not deny_all:
            folder = paths.functions_dir() / "apps" / req["app"]
            for item in req["items"]:
                name = item["function"]["name"]
                if item["problems"] or (approve is not None and name not in approve):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                (folder / f"{name}.json").write_text(json.dumps(item["function"], indent=2))
                installed.append(name)
            if installed:
                self.registry.reload()
        self.publish("import_done", {"id": pid, "installed": installed})
        return {"installed": installed}

    def scan_dropbox(self) -> None:
        """Pick up manifests apps dropped into the imports folder."""
        folder = paths.imports_dir()
        if not folder.is_dir():
            return
        for file in folder.glob("*.json"):
            mtime = file.stat().st_mtime
            if self._seen.get(str(file)) == mtime:
                continue
            self._seen[str(file)] = mtime
            try:
                manifest = json.loads(file.read_text())
                self.submit(manifest, origin=str(file))
            except (OSError, ValueError) as exc:
                self.publish("notice", {"text": f"Couldn't read {file.name}: {exc}"})
            finally:
                done = folder / "processed"
                done.mkdir(exist_ok=True)
                try:
                    file.replace(done / file.name)
                except OSError:
                    pass


__all__ = ["Imports"]
