"""Intention Processing: text -> (function, arguments).

With an intent model loaded, the model reads the Dictionary (only the
agent's enabled functions, plus rated examples) and answers in JSON. Its
answer is validated against the function definitions; a bad answer gets one
retry with the error explained.

Without a model, a keyword matcher does the same job for the built-in
functions and simple custom ones, so Jeeves is usable (and dry-runnable)
before any model is downloaded. Local Response is the default when nothing
else fits.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..functions.base import FunctionDef
from ..functions.registry import Registry
from ..util import normalize, parse_duration, similarity
from .memory import Memory

SYSTEM = """You are the intention processor for a voice assistant named {agent}.
Pick the ONE function below that best matches what the user wants, and fill in its arguments.
Only use functions from this list. Follow each function's argument rules exactly: when an argument
lists allowed values, use one of them; leave out optional arguments you don't need.
If the request is ambiguous or you can't tell what they want, set "function" to null and write a short
clarifying question in "question".

Answer with JSON only:
{{"function": "<name or null>", "args": {{...}}, "confidence": <0.0-1.0>, "question": "<only if unclear>"}}

# Functions
{dictionary}
"""


@dataclass
class Decision:
    function: str | None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    question: str = ""
    method: str = "keywords"          # model | keywords
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntentProcessor:
    def __init__(self, settings: Any, registry: Registry, models: Any, history: Any) -> None:
        self.settings = settings
        self.registry = registry
        self.models = models
        self.history = history
        # () -> {function: [examples]} -- training phrases (training.py)
        self.extra_examples = lambda: {}

    # ---- public ----------------------------------------------------------
    def decide(self, agent: dict[str, Any], text: str, ctx: Any = None) -> Decision:
        functions = [f for f in self.registry.enabled_for(agent) if not self._blocked(f, text)]
        llm = None
        if self.models is not None:
            from ..models.manager import ModelUnavailable
            try:
                llm = self.models.llm("intent", agent)
            except ModelUnavailable as exc:
                if exc.queueable:
                    raise
                llm = None
        if llm is not None and functions:
            return self._with_model(llm, agent, text, functions, ctx)
        return self.keyword_decide(agent, text, functions)

    def validate(self, f: FunctionDef, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        out, problems = {}, []
        args = dict(args or {})
        for a in f.args:
            if a.name in args and args[a.name] not in (None, ""):
                v = args.pop(a.name)
                if a.choices and v not in a.choices:
                    lowered = {str(c).lower(): c for c in a.choices}
                    if str(v).lower() in lowered:
                        v = lowered[str(v).lower()]
                    else:
                        problems.append(f"'{a.name}' must be one of {a.choices}, not {v!r}")
                        continue
                if a.type in ("number",) and isinstance(v, str):
                    try:
                        v = float(v)
                    except ValueError:
                        problems.append(f"'{a.name}' must be a number")
                if a.type == "integer" and not isinstance(v, int):
                    try:
                        v = int(float(v))
                    except (TypeError, ValueError):
                        problems.append(f"'{a.name}' must be a whole number")
                if a.type == "boolean" and isinstance(v, str):
                    v = v.lower() in ("true", "yes", "1", "on")
                if a.type == "duration":
                    try:
                        parse_duration(v)
                    except ValueError:
                        problems.append(f"'{a.name}' must be a duration like '5 minutes'")
                out[a.name] = v
            elif a.required:
                problems.append(f"missing required argument '{a.name}'")
            elif a.default not in (None, "", []):
                out[a.name] = a.default
        # extra args are dropped (models invent them)
        return out, problems

    # ---- model -----------------------------------------------------------
    def _with_model(self, llm: Any, agent: dict[str, Any], text: str, functions: list[FunctionDef],
                    ctx: Any) -> Decision:
        examples = {}
        if self.settings.get("training.intent_examples_from_ratings", True):
            examples = self.history.rated_examples(int(self.settings.get("training.max_examples", 12)))
        for fname, items in self.extra_examples().items():
            examples.setdefault(fname, []).extend(items)
        system = SYSTEM.format(agent=agent.get("name", "Jeeves"),
                               dictionary=self.registry.dictionary_text(functions, examples))
        memory_kind, _ = Memory.detect(text)
        messages = [{"role": "system", "content": system}]
        recent = self.history.recent(int(self.settings.get("memory.recent_count", 3)),
                                     exclude=ctx.request.get("id") if ctx else None)
        if recent:
            messages.append({"role": "system", "content": "Recent requests (for 'that', 'it', 'the one I just "
                             "made'):\n" + "\n".join(f"- \"{r['text']}\" -> {r['function']} {json.dumps(r['args'])}"
                                                     for r in recent)})
        messages.append({"role": "user", "content": text})
        by_name = {f.name: f for f in functions}
        notes: list[str] = []
        for attempt in range(2):
            from ..models.backends import BackendError
            try:
                raw = llm.chat(messages, max_tokens=400, temperature=0.1, json_mode=True,
                               on_token=(lambda t: ctx.think(t, append=True)) if ctx else None,
                               cancelled=ctx.is_cancelled if ctx else None)
            except BackendError as exc:
                notes.append(f"intent model failed ({exc}); used keyword matching")
                d = self.keyword_decide(agent, text, functions)
                d.notes = notes + d.notes
                return d
            data = _parse_json(raw)
            if data is None:
                err = "Your answer wasn't valid JSON."
            else:
                name = data.get("function")
                conf = _num(data.get("confidence"), 0.7)
                if not name:
                    return Decision(None, {}, conf, str(data.get("question") or ""), "model", notes)
                f = by_name.get(name)
                if f is None:
                    err = f"'{name}' isn't one of the available functions."
                else:
                    args, problems = self.validate(f, data.get("args") or {})
                    if not problems:
                        if memory_kind and "remember" in by_name and name != "remember":
                            notes.append(f"also asked to remember ({memory_kind})")
                        return Decision(name, args, conf, "", "model", notes)
                    err = "Problems: " + "; ".join(problems)
            notes.append(err)
            messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": err + " Try again."}]
        return Decision(None, {}, 0.0, "", "model", notes)

    # ---- keyword matcher ---------------------------------------------------
    def _blocked(self, f: FunctionDef, text: str) -> bool:
        t = normalize(text)
        for b in self.registry.blocked(f):
            nb = normalize(b)
            if nb and (nb in t or similarity(nb, t) >= 0.8):
                return True
        return False

    def keyword_decide(self, agent: dict[str, Any], text: str, functions: list[FunctionDef]) -> Decision:
        t = " " + normalize(text) + " "
        by_name = {f.name: f for f in functions}
        best: tuple[float, FunctionDef] | None = None
        for f in functions:
            if f.name == "local_response":
                continue
            for kw in self.registry.keywords(f):
                nk = normalize(kw)
                if nk and f" {nk} " in t:
                    score = len(nk) + (5 if t.strip().startswith(nk) else 0)
                    if best is None or score > best[0]:
                        best = (score, f)
        if best is not None:
            f = best[1]
            args = self._guess_args(f, text, agent)
            args, problems = self.validate(f, args)
            if not problems:
                return Decision(f.name, args, 0.75, "", "keywords")
            if "local_response" not in by_name:
                return Decision(None, {}, 0.3, f"I think you want {f.title}, but: {'; '.join(problems)}. "
                                "Could you say it differently?", "keywords", problems)
        if "local_response" in by_name:
            return Decision("local_response", {"prompt": text}, 0.5, "", "keywords")
        return Decision(None, {}, 0.0, "I'm not sure what you'd like me to do. Could you rephrase that?", "keywords")

    def _guess_args(self, f: FunctionDef, text: str, agent: dict[str, Any]) -> dict[str, Any]:
        low = text.lower()
        if f.name == "timers":
            if "cancel" in low:
                return {"action": "cancel"}
            if re.search(r"\b(list|how long|what timers)\b", low):
                return {"action": "list"}
            m = re.search(r"\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)", low)
            if m:
                rest = low[m.end():].strip(" ,.")
                return {"action": "schedule", "time": m.group(1), "request": rest}
            m = re.search(r"(?:for|in)\s+(.+?)(?:\s+(?:for|called|named)\s+(.+))?$", low)
            if m:
                return {"action": "timer", "duration": m.group(1), "label": (m.group(2) or "").strip(" .")}
            return {"action": "timer"}
        if f.name == "online_prompt":
            for name, key in (("chatgpt", "codex"), ("gpt", "codex"), ("codex", "codex"), ("gemini", "gemini"),
                              ("claude", "claude"), ("grok", "grok")):
                if re.search(rf"\b{name}\b", low):
                    prompt = re.sub(rf"^.*?\b(ask|tell)\s+{name}\b[\s,:]*", "", text, flags=re.I).strip() or text
                    return {"agent": key, "prompt": prompt}
            return {"prompt": text}
        if f.name == "macros":
            action = "list" if re.search(r"\blist\b|what macros", low) else \
                "adjust" if re.search(r"\b(adjust|change|edit|tweak|update)\b", low) else \
                "run" if re.search(r"\b(run|fire|start)\b", low) else "create"
            m = re.search(r"['\"]([^'\"]+)['\"]", text)
            desc = re.sub(r"^.*?\bmacro\b\s*(that|to|which)?\s*", "", text, flags=re.I).strip()
            return {"action": action, "name": m.group(1) if m else "", "description": desc}
        if f.name == "summary":
            action = "start" if re.search(r"\b(start|turn on|begin)\b", low) else \
                "stop" if re.search(r"\b(stop|turn off|end)\b", low) else \
                "status" if "status" in low else "ask" if re.search(r"\bwhat did\b", low) else "summarize"
            m = re.search(r"last\s+(.+?)\s*$", low)
            minutes = None
            if m:
                try:
                    minutes = parse_duration(m.group(1)) / 60
                except ValueError:
                    pass
            return {"action": action, "minutes": minutes, "question": text}
        if f.name == "remember":
            kind, cleaned = Memory.detect(text)
            return {"text": cleaned, "duration": kind or "long_term"}
        if f.name == "handoff":
            agents = self.settings.get("agents", {}) or {}
            for aid, a in agents.items():
                for n in a.get("call_names", []) + [a.get("name", "")]:
                    if n and re.search(rf"\b{re.escape(n.lower())}\b", low) and aid != agent.get("id"):
                        return {"to": aid, "message": text}
            return {"message": text}
        # custom / other functions: give free text to the first string argument
        args: dict[str, Any] = {}
        for a in f.args:
            if a.type in ("string", "command") and a.required:
                args[a.name] = text
                break
        return args


def _parse_json(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _num(v: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default
