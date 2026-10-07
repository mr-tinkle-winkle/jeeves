"""The Dictionary: every function Jeeves knows, where it came from, and the
text the intent model reads.

Sources, in load order (later names never replace earlier ones silently --
a clash is reported and the later one is skipped):

1. built-in partials  (``jeeves.functions.partials``)
2. built-in full functions (``jeeves.functions.builtins``)
3. user partials      (``~/.config/jeeves/partials/*.py``)
4. user full functions (``~/.config/jeeves/functions/*.json``)
5. approved app functions (``~/.config/jeeves/functions/apps/<app>/*.json``)
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any

from .. import paths
from .base import ARG_TYPES, FunctionDef, take_pending
from .composer import validate

log = logging.getLogger("jeeves.functions")

BUILTIN_PARTIAL_MODULES = [
    "jeeves.functions.partials.system",
    "jeeves.functions.partials.desktop",
    "jeeves.functions.partials.screen",
    "jeeves.functions.partials.interaction",
    "jeeves.functions.partials.files",
    "jeeves.functions.partials.memory",
    "jeeves.functions.partials.web",
    "jeeves.functions.partials.youtube",
    "jeeves.functions.partials.flow",
]
BUILTIN_FULL_MODULE = "jeeves.functions.builtins"
BUILTIN_FULL_MODULES = [BUILTIN_FULL_MODULE, "jeeves.functions.apps"]


def _import_defs(module: str) -> list[FunctionDef]:
    take_pending()
    if module in sys.modules:
        mod = importlib.reload(sys.modules[module])
    else:
        mod = importlib.import_module(module)
    defs = take_pending()
    del mod
    return defs


class Registry:
    def __init__(self, settings: Any | None = None) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self.functions: dict[str, FunctionDef] = {}
        self.problems: list[str] = []
        self.reload()

    # ---- loading ---------------------------------------------------------
    def reload(self) -> None:
        with self._lock:
            self.functions, self.problems = {}, []
            for module in BUILTIN_PARTIAL_MODULES:
                for d in _import_defs(module):
                    self._add(d)
            for module in BUILTIN_FULL_MODULES:
                for d in _import_defs(module):
                    self._add(d)
            self._load_user_partials()
            self._load_json_dir(paths.functions_dir(), "user")
            apps = paths.functions_dir() / "apps"
            if apps.is_dir():
                for app_dir in sorted(p for p in apps.iterdir() if p.is_dir()):
                    self._load_json_dir(app_dir, f"app:{app_dir.name}")
            known = set(self.functions)
            for f in self.functions.values():
                if f.steps is not None:
                    for p in validate(f.steps, known):
                        self.problems.append(f"{f.name}: {p}")

    def _add(self, d: FunctionDef) -> None:
        if d.name in self.functions:
            self.problems.append(f"'{d.name}' from {d.source} clashes with an existing function; skipped")
            return
        self.functions[d.name] = d

    def _load_user_partials(self) -> None:
        folder = paths.partials_dir()
        if not folder.is_dir():
            return
        for file in sorted(folder.glob("*.py")):
            take_pending()
            try:
                spec = importlib.util.spec_from_file_location(f"jeeves_user_partials.{file.stem}", file)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception as exc:  # user code: report, don't crash the daemon
                self.problems.append(f"{file.name}: {type(exc).__name__}: {exc}")
                take_pending()
                continue
            for d in take_pending():
                d.source = "user"
                self._add(d)

    def _load_json_dir(self, folder: Path, source: str) -> None:
        if not folder.is_dir():
            return
        for file in sorted(folder.glob("*.json")):
            try:
                data = json.loads(file.read_text())
                self._add(FunctionDef.from_dict(data, source=source))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.problems.append(f"{file.name}: {exc}")

    # ---- queries ---------------------------------------------------------
    def get(self, name: str, agent: dict[str, Any] | None = None) -> FunctionDef | None:
        f = self.functions.get(name)
        if f is None and agent is not None:
            f = next((c for c in self.agent_commands(agent) if c.name == name), None)
        return f

    def agent_commands(self, agent: dict[str, Any]) -> list[FunctionDef]:
        """The agent's own commands (Agents > Commands), as functions."""
        from .commands import command_defs
        return command_defs(agent, set(self.functions))

    def all(self, kind: str | None = None) -> list[FunctionDef]:
        return [f for f in self.functions.values() if kind is None or f.kind == kind]

    def _s(self, path: str, default: Any = None) -> Any:
        return self.settings.get(path, default) if self.settings is not None else default

    def keywords(self, f: FunctionDef) -> list[str]:
        return list(self._s(f"functions.keywords.{f.name}", None) or f.keywords)

    def blocked(self, f: FunctionDef) -> list[str]:
        return list(f.blocked) + list(self._s(f"functions.blocked.{f.name}", []) or [])

    def globally_enabled(self, f: FunctionDef) -> bool:
        override = self._s(f"functions.global_enabled.{f.name}", None)
        return f.default_enabled if override is None else bool(override)

    def enabled_for(self, agent: dict[str, Any]) -> list[FunctionDef]:
        """Full functions this agent may run."""
        out = []
        per_agent = agent.get("functions", {}) or {}
        for f in self.all("full"):
            if f.name in per_agent:
                on = bool(per_agent[f.name])
            else:
                on = self.globally_enabled(f)
            if on:
                out.append(f)
        return out + self.agent_commands(agent)

    # ---- export / save ---------------------------------------------------
    def save_user_function(self, data: dict[str, Any]) -> FunctionDef:
        f = FunctionDef.from_dict(data, source="user")
        if f.steps is None:
            raise ValueError("a user function needs 'steps'")
        problems = validate(f.steps, set(self.functions) | {f.name})
        if problems:
            raise ValueError("; ".join(problems))
        existing = self.functions.get(f.name)
        if existing is not None and existing.source != "user":
            raise ValueError(f"'{f.name}' is a {existing.source} function; pick another name")
        paths.functions_dir().mkdir(parents=True, exist_ok=True)
        (paths.functions_dir() / f"{f.name}.json").write_text(json.dumps(f.to_dict(), indent=2))
        self.reload()
        return self.functions[f.name]

    def delete_user_function(self, name: str) -> None:
        f = self.functions.get(name)
        if f is None or not (f.source == "user" or f.source.startswith("app:")):
            raise ValueError(f"'{name}' isn't a custom function")
        if f.source == "user":
            (paths.functions_dir() / f"{name}.json").unlink(missing_ok=True)
        else:
            (paths.functions_dir() / "apps" / f.source[4:] / f"{name}.json").unlink(missing_ok=True)
        self.reload()

    def export(self, names: list[str] | None = None, app: str = "jeeves-export") -> dict[str, Any]:
        """A manifest other Jeeves installs (or the import popup) accept."""
        chosen = [f for f in self.functions.values()
                  if (names is None and f.source == "user") or (names and f.name in names)]
        for f in chosen:
            if f.steps is None:
                raise ValueError(f"'{f.name}' is a Python function and can't be exported as a composition"
                                 + (" (copy its .py file instead)" if f.source == "user" else ""))
        return {"jeeves_manifest": 1, "app": app, "functions": [f.to_dict() for f in chosen]}

    # ---- dictionary text -------------------------------------------------
    def describe(self, f: FunctionDef, examples: list[dict[str, Any]] | None = None) -> str:
        lines = [f"### {f.name}  ({f.kind}{'' if f.source == 'builtin' else ', ' + f.source})", f.description]
        if f.how:
            lines.append(f"How it works: {f.how}")
        kws = self.keywords(f)
        if kws:
            lines.append("Keywords: " + ", ".join(repr(k) for k in kws))
        if f.args:
            lines.append("Arguments:")
            for a in f.args:
                kind = f"one of {a.choices}" if a.choices else f"{a.type} ({ARG_TYPES.get(a.type, a.type)})"
                req = "required" if a.required else f"optional, default {a.default!r}"
                said = " Filled in from what you say." if a.said else ""
                lines.append(f"  - {a.name}: {kind}; {req}. {a.description}{said}")
        if f.returns:
            lines.append(f"Returns: {f.returns}")
        for ex in f.examples:
            lines.append(f"Example: {ex}")
        blocked = self.blocked(f)
        if blocked:
            lines.append("Never use this function for: " + "; ".join(blocked))
        for ex in examples or []:
            tag = "GOOD" if ex.get("good") else "BAD"
            extra = f" -> should have used {ex['should_use']}" if ex.get("should_use") else ""
            note = f" ({ex['comment']})" if ex.get("comment") else ""
            lines.append(f"Rated {tag}: \"{ex['text']}\"{extra}{note}")
        return "\n".join(lines)

    def brief(self, f: FunctionDef, examples: list[dict[str, Any]] | None = None) -> str:
        """The function as the intent model needs it: what it's for, the arguments it has to fill in and
        a few examples. Not how it works inside, and not the request itself: arguments that are the
        user's own words are filled in from what was said (Arg.said), so the model never rewrites them."""
        lines = [f"### {f.name}: {f.description}"]
        for a in f.args:
            if a.said:
                continue
            if a.choices:
                kind = "|".join(str(c) for c in a.choices if c not in ("", None))
            elif a.type in ("duration", "time"):
                kind = ARG_TYPES[a.type]
            else:
                kind = {"boolean": "true/false", "integer": "whole number", "number": "number"}.get(a.type, "text")
            if a.required:
                need = "required"
            elif a.default not in (None, "", [], False):
                need = f"default {a.default}"
            else:
                need = "optional"
            desc = a.description
            filler = {str(c).lower() for c in a.choices or []} | {"or", "and", "what", "to", "do", "the", "a"}
            if set(re.findall(r"[\w-]+", desc.lower())) <= filler:
                desc = ""                       # "What to do", "start or stop": the values already say it
            lines.append(f"- {a.name} ({kind}; {need})" + (f": {desc}" if desc else ""))
        said = [re.sub(r"^\w+,\s+", "", ex) for ex in f.examples][:3]    # "Jeeves, set a timer" -> "set a timer"
        if said:
            lines.append("e.g. " + " | ".join(f'"{ex}"' for ex in said))
        elif self.keywords(f):
            lines.append("e.g. " + " | ".join(f'"{k}"' for k in self.keywords(f)[:4]))
        blocked = self.blocked(f)
        if blocked:
            lines.append("Never for: " + "; ".join(blocked))
        for ex in examples or []:
            if ex.get("good"):
                lines.append(f'Right for: "{ex["text"]}"')
            elif ex.get("should_use"):
                lines.append(f'Wrong for: "{ex["text"]}" (use {ex["should_use"]})')
            else:
                lines.append(f'Wrong for: "{ex["text"]}"' + (f" ({ex['comment']})" if ex.get("comment") else ""))
        return "\n".join(lines)

    def dictionary_text(self, functions: list[FunctionDef], examples: dict[str, list[dict[str, Any]]] | None = None,
                        brief: bool = False) -> str:
        examples = examples or {}
        describe = self.brief if brief else self.describe
        return "\n\n".join(describe(f, examples.get(f.name)) for f in functions)

    def to_json(self) -> list[dict[str, Any]]:
        out = []
        for f in self.functions.values():
            d = f.to_dict()
            d["effective_keywords"] = self.keywords(f)
            d["effective_blocked"] = self.blocked(f)
            d["globally_enabled"] = self.globally_enabled(f)
            out.append(d)
        return out
