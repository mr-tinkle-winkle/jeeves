"""FunctionContext: what a running function can do.

One context per request. Functions (built-in, user partials, compositions)
only ever touch the outside world through this object, which is what makes
Dry Run, Abort, the trace in Manual Response Review, and per-agent rules
possible.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..functions.base import Cancelled, FunctionDef, FunctionError
from ..functions.composer import run_steps
from ..util import truncate


class FunctionContext:
    def __init__(self, engine: Any, agent_id: str, agent: dict[str, Any], entry: dict[str, Any],
                 dry_run: bool = False, depth: int = 0) -> None:
        self.engine = engine
        self.agent_id = agent_id
        self.agent = dict(agent, id=agent_id)
        self.entry = entry                    # the history entry being filled in
        self.request = entry                  # alias: {id, text, source, ...}
        self.dry_run = dry_run
        self.depth = depth
        self.settings = engine.settings
        self.cancel_event = threading.Event()
        self.suspend_event = threading.Event()      # set = suspended (right-click > Suspend)
        self.spoke = False
        self.thoughts: list[str] = []
        self.looking_at: str = ""
        self.playback = None
        self.saying = ""                                # the sentence being spoken right now
        self.stage = "thinking"

    # ---- cancellation ------------------------------------------------------
    def gate(self) -> None:
        """Block here while the request is suspended (until resumed or closed)."""
        while self.suspend_event.is_set() and not self.cancel_event.is_set():
            self.cancel_event.wait(0.1)

    def is_cancelled(self) -> bool:
        self.gate()          # also pauses model output streams while suspended
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        self.gate()
        if self.cancel_event.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self.cancel_event.set()
        if self.playback is not None:
            self.playback.stop()

    def wait(self, seconds: float) -> None:
        if self.dry_run:
            self.trace("wait", seconds=seconds)
            return
        if self.cancel_event.wait(max(0.0, float(seconds))):
            raise Cancelled()
        self.check_cancelled()

    # ---- trace / indicator ---------------------------------------------------
    def trace(self, kind: str, **data: Any) -> None:
        item = {"t": round(time.time() - self.entry["time"], 3), "kind": kind}
        item.update({k: (truncate(v, 2000) if isinstance(v, str) else v) for k, v in data.items()})
        self.entry["trace"].append(item)
        self.engine.publish("trace", {"request": self.entry["id"], "item": item})

    def state(self, stage: str, detail: str = "", **extra: Any) -> None:
        self.stage = stage
        if not self.dry_run:
            self.engine.set_indicator(self.entry["id"], self.agent_id, stage, detail, **extra)
        self.trace("stage", stage=stage, detail=detail)

    def think(self, text: str, append: bool = False, looking_at: str | None = None) -> None:
        if append and self.thoughts:
            self.thoughts[-1] += text
        else:
            self.thoughts.append(text)
        if looking_at is not None:
            self.looking_at = looking_at
        self.engine.publish("thoughts", {"request": self.entry["id"], "text": text, "append": append,
                                         "looking_at": self.looking_at})

    # ---- output ------------------------------------------------------------
    def show(self, text: str) -> None:
        self.engine.publish("response", {"request": self.entry["id"], "agent": self.agent_id, "text": text})

    def say(self, text: str) -> str:
        text = str(text).strip()
        if not text:
            return text
        self.entry["response"] = (self.entry["response"] + "\n" + text).strip() if self.entry["response"] else text
        self.trace("say", text=text)
        if self.dry_run:
            return text
        self.spoke = True
        self.state("responding", text)
        if self.agent.get("show_output", True):
            self.show(text)
        self.engine.speak(self, text)
        return text

    def notify(self, title: str, body: str = "") -> None:
        self.trace("notify", title=title, body=body)
        if not self.dry_run:
            from ..functions.partials.system import notify
            notify(title, body, app=self.agent.get("name", "Jeeves"))

    # ---- input ---------------------------------------------------------------
    def ask(self, question: str, choices: list[str] | None = None, timeout: float = 60.0) -> str | None:
        self.trace("ask", question=question, choices=choices)
        if self.dry_run:
            return f"<answer to: {question}>"
        self.say(question)
        answer = self.engine.wait_for_answer(self, question, choices, timeout)
        self.trace("answer", text=answer)
        return answer

    def confirm(self, text: str, hint: str = "") -> bool:
        self.trace("confirm", text=text)
        if self.dry_run:
            return True
        ok = self.engine.wait_for_confirm(self, text, hint)
        self.trace("confirmed" if ok else "declined", text=text)
        return ok

    def wait_for_click(self, text: str, timeout: float = 300.0) -> bool:
        if self.dry_run:
            return True
        return self.engine.wait_for_confirm(self, text, "", timeout=timeout, click_only=True)

    # ---- calling functions ---------------------------------------------------
    def call(self, name: str, **args: Any) -> Any:
        self.check_cancelled()
        f: FunctionDef | None = self.engine.registry.get(name)
        if f is None:
            raise FunctionError(f"there's no function called '{name}'")
        if self.depth > 12:
            raise FunctionError("functions are calling each other too deeply")
        if f.kind == "full" and self.depth > 0:
            allowed = {x.name for x in self.engine.registry.enabled_for(self.agent)}
            if name not in allowed:
                raise FunctionError(f"'{name}' isn't enabled for {self.agent.get('name')}")
        validated, problems = self.engine.intent.validate(f, args)
        if problems:
            raise FunctionError(f"{name}: " + "; ".join(problems))
        self.trace("call", function=name, args=validated)
        if self.dry_run and f.kind == "partial" and not f.dry_run_safe:
            placeholder = f"<{name} result>"
            self.trace("would_run", function=name, args=validated)
            return placeholder
        self.depth += 1
        try:
            if f.steps is not None:
                variables = dict(validated)
                variables["args"] = dict(validated)
                variables["agent"] = self.agent.get("name")
                variables["request"] = self.entry.get("text", "")
                result = run_steps(f.steps, self, variables)
            elif f.impl is not None:
                result = f.impl(self, **validated)
            else:
                raise FunctionError(f"'{name}' has nothing to run")
        finally:
            self.depth -= 1
        self.trace("result", function=name, result=result if isinstance(result, (int, float, bool)) or result is None
                   else truncate(result if isinstance(result, str) else _json(result), 1500))
        return result

    def register_trigger(self, spec: dict[str, Any], steps: list, variables: dict[str, Any]) -> str:
        if self.dry_run:
            self.trace("would_register_trigger", spec=spec)
            return "<trigger>"
        tid = self.engine.triggers.add_steps(spec, self.agent_id, steps, variables)
        self.trace("trigger", id=tid, spec=spec)
        return tid


def _json(v: Any) -> str:
    import json
    try:
        return json.dumps(v, default=str)
    except (TypeError, ValueError):
        return str(v)
