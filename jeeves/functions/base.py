"""Function definitions -- the entries of the Dictionary.

Every function, full or partial, explicitly defines itself: what it does, how
it works, which specific values its arguments accept (variations) and which
types of values the open-ended ones take (app names, durations, ...). The
intent model only ever sees this text, so it has to be complete.

* **Partial functions** are small Python callables (built in, or user-written
  in ``~/.config/jeeves/partials/*.py``).
* **Full functions** are what agents run. Built-in ones are Python; user-made
  ones are compositions of partials (``steps``, see ``composer.py``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

ARG_TYPES = {
    "string": "free text",
    "number": "a number",
    "integer": "a whole number",
    "boolean": "true or false",
    "duration": "a length of time, e.g. '5 minutes', '90s', '1h30m'",
    "time": "a clock time or date, e.g. '7:30 pm', 'tomorrow 9am'",
    "app": "an application name as shown in the window list, e.g. 'firefox', 'obs'",
    "agent": "the name of one of your agents",
    "key": "a Linux key name, e.g. KEY_A, KEY_LEFTCTRL, BTN_LEFT",
    "path": "a file path",
    "url": "a web address",
    "command": "a program and its arguments, no shell syntax",
    "list": "a list of values",
    "object": "a JSON object",
    "position": "screen coordinates {x, y} in pixels",
    "region": "a screen region: 'top', 'middle', 'bottom-left', ... or {x, y, w, h}",
    "function": "the name of a Jeeves function",
    "macro": "the name of a Puppetry macro",
}


@dataclass
class Arg:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: Any = None
    # specific values this argument can take (function variations); when set,
    # the intent model must pick one of them
    choices: list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Arg":
        return cls(**{k: d[k] for k in ("name", "type", "description", "required", "default", "choices") if k in d})


@dataclass
class FunctionDef:
    name: str
    kind: str                         # "full" | "partial"
    title: str = ""
    description: str = ""             # what it does
    how: str = ""                     # how it works
    args: list[Arg] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    returns: str = ""
    default_enabled: bool = True
    category: str = "general"
    source: str = "builtin"           # builtin | user | app:<name>
    impl: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    steps: list[dict[str, Any]] | None = None
    # partials this function uses (shown on import approval); computed for compositions
    uses: list[str] = field(default_factory=list)
    # safe to call during a dry run (pure reads); others are described instead
    dry_run_safe: bool = False

    def __post_init__(self) -> None:
        if not self.title:
            self.title = self.name.replace("_", " ").title()
        if self.steps is not None and not self.uses:
            from .composer import partials_used
            self.uses = sorted(partials_used(self.steps))

    def arg(self, name: str) -> Arg | None:
        return next((a for a in self.args if a.name == name), None)

    def to_dict(self, include_impl: bool = False) -> dict[str, Any]:
        d = {
            "name": self.name, "kind": self.kind, "title": self.title, "description": self.description,
            "how": self.how, "args": [a.to_dict() for a in self.args], "keywords": list(self.keywords),
            "examples": list(self.examples), "blocked": list(self.blocked), "returns": self.returns,
            "default_enabled": self.default_enabled, "category": self.category, "source": self.source,
            "steps": self.steps, "uses": list(self.uses), "dry_run_safe": self.dry_run_safe,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str | None = None) -> "FunctionDef":
        return cls(
            name=d["name"], kind=d.get("kind", "full"), title=d.get("title", ""),
            description=d.get("description", ""), how=d.get("how", ""),
            args=[Arg.from_dict(a) for a in d.get("args", [])], keywords=list(d.get("keywords", [])),
            examples=list(d.get("examples", [])), blocked=list(d.get("blocked", [])),
            returns=d.get("returns", ""), default_enabled=bool(d.get("default_enabled", True)),
            category=d.get("category", "custom"), source=source or d.get("source", "user"),
            steps=d.get("steps"), uses=list(d.get("uses", [])),
        )


class FunctionError(RuntimeError):
    """Raised by a function to report a user-facing failure."""


class Cancelled(Exception):
    """The request was aborted (Abort key) while a function was running."""


# ---------------------------------------------------------------------------
# Decorators used by built-in and user partial modules
# ---------------------------------------------------------------------------

_PENDING: list[FunctionDef] = []


def partial(name: str, description: str, args: list[Arg] | None = None, how: str = "", returns: str = "",
            keywords: list[str] | None = None, examples: list[str] | None = None, category: str = "general",
            dry_run_safe: bool = False, title: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a partial function. The callable receives ``ctx`` (a
    ``FunctionContext``) followed by its arguments as keywords.

    Example (``~/.config/jeeves/partials/weather.py``)::

        from jeeves.functions import partial, Arg

        @partial("get_weather", "Gets the forecast for a city.",
                 args=[Arg("city", "string", "City name")], returns="forecast text")
        def get_weather(ctx, city):
            return ctx.call("request_website", url=f"https://wttr.in/{city}?format=3")
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _PENDING.append(FunctionDef(
            name=name, kind="partial", title=title, description=description, how=how, args=args or [],
            keywords=keywords or [], examples=examples or [], returns=returns, category=category,
            impl=fn, dry_run_safe=dry_run_safe,
        ))
        return fn
    return deco


def full(name: str, description: str, args: list[Arg] | None = None, how: str = "", keywords: list[str] | None = None,
         examples: list[str] | None = None, default_enabled: bool = True, category: str = "general",
         blocked: list[str] | None = None, uses: list[str] | None = None, returns: str = "",
         title: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a built-in full function implemented in Python."""
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        _PENDING.append(FunctionDef(
            name=name, kind="full", title=title, description=description, how=how, args=args or [],
            keywords=keywords or [], examples=examples or [], default_enabled=default_enabled,
            category=category, blocked=blocked or [], impl=fn, uses=uses or [], returns=returns,
        ))
        return fn
    return deco


def take_pending() -> list[FunctionDef]:
    out = list(_PENDING)
    _PENDING.clear()
    return out
