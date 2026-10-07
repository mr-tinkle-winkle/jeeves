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
from ..config import agent_memory
from .memory import Memory

SYSTEM = """Pick the function that handles this request to {agent}, a voice assistant, and fill in its arguments.
Use only the functions below. For an argument with listed values, use one of them; leave out arguments you
don't need. Take the likeliest meaning: people speak casually. Only if it can't be done without a missing
detail (a timer with no length), reply {{"function": null, "question": "<ask for just that detail>"}}.{research}
Reply with JSON only: {{"function": "<name>", "args": {{...}}}}

{dictionary}
"""
RECENT_SECONDS = 15 * 60       # older requests aren't what "it" or "that" means
RESEARCH_RULE = "\nFactual questions (games, products, people, places, news, prices, dates) go to research."

# instructions read word for word ("click Save"): "could you ... please" is in the way
POLITE_STRIP = {"control_mode"}
# Words in front of a request that only say "look it up" / "ask X": not part of the question itself
SAID_PREFIX = {
    "research": r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:look\s+up|search\s+(?:the\s+web\s+|online\s+)?"
                r"(?:for\s+)?|research|google|find\s+out|check)\s+(?:online\s+)?",
    "online_prompt": r"^(?:please\s+)?(?:can\s+you\s+)?(?:ask|tell)\s+(?:chat\s*gpt|gpt|codex|gemini|claude|grok)\b"
                     r"(?:\s+(?:to|about|if|whether))?[\s,:]*",
}


def agent_id_of(ctx: Any) -> str | None:
    return getattr(ctx, "agent_id", None) if ctx is not None else None


@dataclass
class Decision:
    function: str | None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    question: str = ""
    method: str = "keywords"          # model | keywords | rules
    notes: list[str] = field(default_factory=list)
    refusal: str = ""                 # say this instead of running anything (e.g. the function is off)

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
        ruled = self.rule_decide(agent, text, functions)
        if ruled is not None:
            return self.with_said(ruled, agent, text)
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
            return self.with_said(self._with_model(llm, agent, text, functions, ctx), agent, text)
        return self.with_said(self.keyword_decide(agent, text, functions), agent, text)

    # ---- the request in the user's own words ------------------------------
    def said_text(self, f: FunctionDef, agent: dict[str, Any], text: str) -> str:
        """What the user said, as the function's request: without the agent's name, and without the
        words that only pick the function ("look up", "ask Gemini")."""
        t = self.strip_address(agent, text, polite=f.name in POLITE_STRIP)
        prefix = SAID_PREFIX.get(f.name)
        if prefix:
            t = re.sub(prefix, "", t, flags=re.I).strip() or t
        return t.strip()

    def said_args(self, f: FunctionDef, agent: dict[str, Any], text: str) -> dict[str, str]:
        return {a.name: self.said_text(f, agent, text) for a in f.args if a.said}

    def with_said(self, d: Decision, agent: dict[str, Any], text: str) -> Decision:
        """The decision with its request arguments set to what was said -- whoever decided (rules,
        keywords, the model), the function gets the question, not a rewording of it."""
        f = self.registry.get(d.function, agent) if d.function else None
        if f is not None:
            d.args.update(self.said_args(f, agent, text))
        return d

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
                               research=RESEARCH_RULE if any(f.name == "research" for f in functions) else "",
                               dictionary=self.registry.dictionary_text(functions, examples, brief=True))
        memory_kind, _ = Memory.detect(text)
        messages = [{"role": "system", "content": system}]
        recent = self._recent(agent, text, ctx)
        if recent:
            me = ctx.agent_id if ctx is not None else None
            names = {k: v.get("name", k) for k, v in (self.settings.get("agents", {}) or {}).items()}

            def line(r: dict[str, Any]) -> str:
                to = "" if r.get("agent") in (None, me) else f" (to {names.get(r['agent'], 'another assistant')})"
                f = self.registry.get(r.get("function") or "", agent)
                said = {a.name for a in f.args if a.said} if f is not None else set()
                args = {k: v for k, v in (r.get("args") or {}).items() if v not in (None, "", [], False)
                        and k not in said}
                return f"- \"{r['text']}\"{to} -> {r['function']}" + (f" {json.dumps(args)}" if args else "")
            messages.append({"role": "system", "content": "Earlier requests, only for what \"it\" or \"that\" "
                             "refers to:\n" + "\n".join(line(r) for r in recent)})
        messages.append({"role": "user", "content": self.strip_address(agent, text, polite=False) or text})
        by_name = {f.name: f for f in functions}
        notes: list[str] = []
        for _attempt in range(2):
            from ..models.backends import BackendError
            try:
                from ..models import jeenius
                think = jeenius.think_for(jeenius.level(agent, self.settings), "intent", text)
                raw = llm.chat(messages, max_tokens=400, temperature=0.1, json_mode=True, think=think,
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
                    given = data.get("args") if isinstance(data.get("args"), dict) else {}
                    args, problems = self.validate(f, {**given, **self.said_args(f, agent, text)})
                    if not problems:
                        if memory_kind and "remember" in by_name and name != "remember":
                            notes.append(f"also asked to remember ({memory_kind})")
                        return Decision(name, args, conf, "", "model", notes)
                    err = "Problems: " + "; ".join(problems)
            notes.append(err)
            messages += [{"role": "assistant", "content": raw}, {"role": "user", "content": err + " Try again."}]
        return Decision(None, {}, 0.0, "", "model", notes)

    def _recent(self, agent: dict[str, Any], text: str, ctx: Any) -> list[dict[str, Any]]:
        """Earlier requests, only when this one points back at them ("pause it", "the macro I just
        made"), and only recent ones: otherwise they're noise the model may act on again."""
        from ..util import refers_back
        am = agent_memory(agent, self.settings)
        if not am["recent"] or not refers_back(self.strip_address(agent, text, polite=False)):
            return []
        n = min(3, am["recent"])
        if ctx is not None and hasattr(ctx.engine, "recent_for"):
            recent = ctx.engine.recent_for(ctx.agent_id, agent, n, am["own_only"], ctx.request.get("id"))
        else:
            recent = self.history.recent(n, agent=agent_id_of(ctx) if am["own_only"] else None,
                                         exclude=ctx.request.get("id") if ctx else None)
        import time as _time
        return [r for r in recent if _time.time() - float(r.get("time") or 0) < RECENT_SECONDS]

    # ---- keyword matcher ---------------------------------------------------
    def _blocked(self, f: FunctionDef, text: str) -> bool:
        t = normalize(text)
        for b in self.registry.blocked(f):
            nb = normalize(b)
            if nb and (nb in t or similarity(nb, t) >= 0.8):
                return True
        return False

    # ---- unmistakable requests -------------------------------------------
    def strip_address(self, agent: dict[str, Any], text: str, polite: bool = True) -> str:
        """'Jeeves, could you please click Save' -> 'click Save'."""
        t = text.strip()
        names = [n for n in (agent.get("call_names") or []) + [agent.get("name", "")] if n]
        for n in sorted(names, key=len, reverse=True):
            t = re.sub(rf"^\W*(hey\s+|ok\s+|okay\s+)?{re.escape(n)}\b[\s,.:!-]*", "", t, flags=re.I)
        if not polite:
            return t.strip(" .!")
        t = re.sub(r"^(please\s+|can you\s+|could you\s+|would you\s+|will you\s+)+", "", t, flags=re.I)
        return re.sub(r"\s+please[.!?]*$", "", t, flags=re.I).strip(" .!?")

    def rule_decide(self, agent: dict[str, Any], text: str, functions: list[FunctionDef]) -> Decision | None:
        """Requests whose meaning is unmistakable skip the model: small intent models
        often answered "click Save" or "what's on my screen" with a chat reply."""
        by_name = {f.name: f for f in functions}
        core = self.strip_address(agent, text)
        low = core.lower()
        name = agent.get("name", "Jeeves")
        if "youtube" in by_name:
            m = re.match(r"^(pause|resume|unpause|stop|close)\s+(the\s+)?(video|youtube)\b", low)
            if m:
                act = {"unpause": "resume", "close": "stop"}.get(m.group(1), m.group(1))
                return Decision("youtube", {"action": act}, 0.95, "", "rules")
            if getattr(self, "video_playing", lambda: False)():
                m = re.match(r"^(?:go\s+to\s+the\s+|skip\s+to\s+the\s+)?(next|previous|last)\s+chapter$|"
                             r"^(speed\s+(?:it\s+)?up|faster|slow\s+(?:it\s+)?down|slower)$", low.strip(" .!"))
                if m:
                    act = ("next_chapter" if m.group(1) == "next" else "previous_chapter") if m.group(1) else \
                        ("faster" if re.search(r"up|faster", m.group(2)) else "slower")
                    return Decision("youtube", {"action": act}, 0.9, "", "rules")
            m = re.match(r"^(skip|go|jump|fast forward|rewind|go back)\s*(ahead|forward|back(wards?)?)?\s*(\d+)?\s*"
                         r"(seconds?|secs?|minutes?|mins?)?$", low)
            if m and getattr(self, "video_playing", lambda: False)():
                secs = float(m.group(4) or 10) * (60 if (m.group(5) or "").startswith("min") else 1)
                back = "back" in low or "rewind" in low
                return Decision("youtube", {"action": "back" if back else "forward", "seconds": secs}, 0.9, "", "rules")
            if re.search(r"\byoutube\b", low) or re.search(
                    r"\b(pull up|put on|play|show me|find)\b.*\b(video|vid|upload)s?\b", low) or re.search(
                    r"\b(newest|latest|most recent)\s+(video|upload|vid)\b", low):
                from ..functions.partials.youtube import parse_request
                args = parse_request(core)
                args.update(action="play")
                return Decision("youtube", args, 0.95, "", "rules")
        if "watch_screen" in by_name:
            if re.search(r"\b(stop|quit|end|cancel|enough)\s+(the\s+)?(watching|commentary|commentating)\b", low):
                return Decision("watch_screen", {"action": "stop"}, 0.95, "", "rules")
            if re.match(r"^(watch|keep an eye on|start watching|commentate|(give|do)\s+(me\s+)?(some\s+)?(live\s+)?"
                        r"commentary)\b", low) or re.search(r"\bwatch\s+(my|the)\s+(screen|game|stream|match)\b", low):
                m = re.search(r"\b(?:and\s+)?(?:tell|let|warn|alert|notify)\s+me\s+(?:know\s+)?((?:when|if)\s+.+)$", low)
                return Decision("watch_screen", {"action": "start", "screen": guess_screen(low) or "all",
                                                 "focus": m.group(1) if m else ""}, 0.95, "", "rules")
        if re.match(CONTROL_PATTERN, low):
            if "control_mode" in by_name:
                return Decision("control_mode", {"instruction": core}, 0.9, "", "rules")
            if self.registry.get("control_mode") is not None:
                return Decision(None, {}, 0.9, "", "rules", refusal=(
                    f"Control Mode is turned off for {name}, so I can't use the keyboard or mouse. Turn it on "
                    f"in Settings > Agents > {name} > Functions."))
        if re.search(SCREEN_PATTERN, low) or re.search(SCREEN_PATTERN, text.lower()):
            if "screen_reading" in by_name:
                return Decision("screen_reading", {"question": self.strip_address(agent, text, polite=False),
                                                   "region": guess_region(low), "screen": guess_screen(low)}, 0.9, "",
                                "rules")
            if self.registry.get("screen_reading") is not None:
                return Decision(None, {}, 0.9, "", "rules", refusal=(
                    f"Screen Reading is turned off for {name}. Turn it on in Settings > Agents > {name} > "
                    "Functions."))
        if "research" in by_name and self.settings.get("research.auto_for_facts", True):
            from ..functions import custom_sources
            from .engine import looks_factual
            if looks_factual(core, agent) or custom_sources.matches(agent, core) and QUESTION.match(low):
                # specific facts get looked up, not guessed -- and so does anything one of your sites is for
                return Decision("research", {"question": core, "depth": "quick"}, 0.85, "", "rules")
        if re.match(r"^at\s+\d{1,2}(:\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?\b", low) and "timers" in by_name:
            args, problems = self.validate(by_name["timers"], self._guess_args(by_name["timers"], core, agent))
            if not problems:
                return Decision("timers", args, 0.9, "", "rules")
        return None

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
        if f.name == "research":
            q = re.sub(r"^\W*(please\s+)?(look\s+up|search\s+(the\s+web\s+)?for|research|google|find\s+out)\s*",
                       "", text, flags=re.I).strip(" ?.")
            return {"question": q or text}
        if f.name == "screen_reading":
            return {"question": self.strip_address(agent, text), "region": guess_region(low),
                    "screen": guess_screen(low)}
        if f.name == "control_mode":
            return {"instruction": self.strip_address(agent, text)}
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


QUESTION = re.compile(r"^(who|what|when|where|which|why|how|is|are|does|do|did|can|could|should|was|were|tell me|"
                      r"explain)\b")
CONTROL_PATTERN = (r"^(left[- ]|right[- ]|middle[- ]|double[- ])?click\b|^(press|hit|tap)\s+(the\s+)?\S|"
                   r"^(hold|hold down|release|let go)\b|^type\s+\S|^scroll\s+(up|down|left|right)\b|"
                   r"^(move|put)\s+(the\s+)?(mouse|cursor|pointer)\b|^(take control|use the (mouse|keyboard))\b")
SCREEN_PATTERN = (r"\b(on|of|at|in)\s+(my|the|this|that)\s+(\w+\s+)?(screen|monitor|display)s?\b|"
                  r"\b(on|of|at|across)\s+(all|both|each|every)\s+(of\s+)?(my\s+|the\s+)?(screens|monitors|displays)\b|"
                  r"\bread\s+(out\s+)?(the|my|this|that|what)\b.*\b(screen|page|window|error|message|says?|text|popup|dialog)\b|"
                  r"\bread\s+(the|my)\s+screen\b|\bwhat\s+(does|do)\s+(it|(this|that|the)(\s+\w+){0,2})\s+say\b|"
                  r"\bwhat\s+(is|'s)\s+(this|that)\s+(error|message|popup|dialog|window)\b|"
                  r"\b(can|do)\s+you\s+see\s+(my|the)\s+screen\b|\blook\s+at\s+(my|the|this)\s+screen\b|"
                  r"\bwhat\s+am\s+i\s+looking\s+at\b|"
                  r"\bwhere('s|\s+is)\s+the\s+.+\s+(button|icon|link|tab|error|message|window|menu|field|box|popup)\b|"
                  # what's going on in an app that's open: a call, a server's members, a chat
                  r"\b(who|what|how many)\b.*\b(discord|voice (chat|channel)|(in|on) (a|the|my|this) (call|server|chat|lobby)|"
                  r"call with|online right now|in my (call|server|chat|lobby))\b")


def guess_screen(low: str) -> str:
    """'on my left monitor' -> 'left'; '' = let the function decide."""
    if re.search(r"\b(all|both|every|each)\s+(of\s+)?(my\s+|the\s+)?(screens|monitors|displays)\b", low):
        return "all"
    m = re.search(r"\b(left|right|middle|center|centre|main|primary|first|second|third|other|top|bottom)\s+"
                  r"(screen|monitor|display)\b", low)
    return m.group(1) if m else ""


def guess_region(low: str) -> str:
    """'read the top of my screen' -> 'top'. Words naming a monitor ('my left screen') don't count."""
    mon = r"(?!\s+(?:screen|monitor|display)s?\b)"
    for r in ("top-left", "top-right", "bottom-left", "bottom-right"):
        if re.search(rf"\b{r.replace('-', '[ -]')}\b{mon}", low):
            return r
    for word, r in (("top", "top"), ("bottom", "bottom"), ("left", "left"), ("right", "right"),
                    ("middle", "middle"), ("center", "middle"), ("centre", "middle")):
        if re.search(rf"\bthe\s+{word}\b{mon}", low):
            return r
    return "anywhere"


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
