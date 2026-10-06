"""Jump in whenever the AI wants to.

An agent with this on hears the conversation on its sources (it is transcribed
continuously, like the Summary log) and, after things are said that weren't
addressed to anyone, may decide to chime in with its own voice.

``frequency`` (0..1) sets how talkative it is:
  * 1   -- a full part of the conversation: considers every line, short cooldown,
           told to join in the way a friend in the call would;
  * 0.5 -- speaks up when it has something useful, funny or relevant;
  * 0.1 -- only for something important (a question nobody answered, a clear
           mistake it can correct);
  * 0   -- never.
The model always decides last: it answers PASS when it has nothing worth saying.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from typing import Any

from ..util import similarity

log = logging.getLogger("jeeves.jumpin")
WINDOW_SECONDS = 180


def cooldown(frequency: float) -> float:
    """Seconds an agent waits after speaking before it may jump in again."""
    return 8.0 + (1.0 - frequency) * 172.0


def consider_chance(frequency: float) -> float:
    """Chance that a new line makes the agent think about jumping in at all."""
    return min(1.0, frequency * 1.5)


def style(frequency: float) -> str:
    if frequency >= 0.8:
        return ("You are a full participant in this conversation, like a friend in the call. Join in naturally: "
                "react, answer, joke, add to what people say. Don't talk over everyone or repeat yourself, and "
                "answer PASS when nothing calls for a reply.")
    if frequency >= 0.4:
        return ("Speak up when you have something genuinely useful, relevant or funny to add. Otherwise answer "
                "PASS. Most lines should get PASS.")
    return ("Only interrupt for something important: a question nobody answered that you can, a clear factual "
            "mistake, or something urgent. Otherwise answer PASS. Almost every line should get PASS.")


class JumpIn:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        # (time, who, text, source): source = where it was heard, or "agent:<id>" for an agent's own line
        self.lines: deque[tuple[float, str, str, str]] = deque()
        self.last_spoke: dict[str, float] = {}
        self.last_said: dict[str, str] = {}
        self.busy: set[str] = set()
        self._lock = threading.Lock()

    # ---- who wants what ---------------------------------------------------
    def agents(self) -> dict[str, dict[str, Any]]:
        out = {}
        for aid, a in self.engine.active_agents().items():
            j = a.get("jump_in") or {}
            if j.get("enabled") and float(j.get("frequency", 0)) > 0:
                out[aid] = a
        return out

    def wants(self, source: str) -> bool:
        """Does some agent need this source transcribed for jumping in?"""
        if not self.engine.is_on():
            return False
        return any(self.engine._listens(a, source) for a in self.agents().values())

    # ---- the conversation -------------------------------------------------
    @staticmethod
    def _who(source: str) -> str:
        return "User" if source == "microphone" else "Others (voice chat / computer audio)"

    def heard(self, source: str, text: str) -> None:
        if not text.strip():
            return
        now = time.time()
        with self._lock:
            # the agent's own voice coming back through the mic is not new conversation
            if any(similarity(text, said) > 0.8 for said in self.last_said.values()):
                return
            self.lines.append((now, self._who(source), text.strip(), source))
            while self.lines and self.lines[0][0] < now - WINDOW_SECONDS:
                self.lines.popleft()

    def spoke(self, agent_id: str, text: str) -> None:
        agent = self.engine.agents().get(agent_id, {})
        with self._lock:
            self.last_spoke[agent_id] = time.time()
            self.last_said[agent_id] = text
            self.lines.append((time.time(), agent.get("name", agent_id), text.strip(), f"agent:{agent_id}"))

    def can_hear(self, agent_id: str, agent: dict[str, Any], source: str) -> bool:
        """Only what this agent could actually hear: its own sources, its own lines, and other
        agents' lines when it listens to the speakers (desktop audio)."""
        if source.startswith("agent:"):
            return source == f"agent:{agent_id}" or agent.get("listen_to") in ("desktop", "both")
        return self.engine._listens(agent, source)

    def transcript(self, agent_id: str | None = None, agent: dict[str, Any] | None = None) -> str:
        with self._lock:
            lines = list(self.lines)
        if agent_id is not None and agent is not None:
            lines = [ln for ln in lines if self.can_hear(agent_id, agent, ln[3])]
        name = (agent or {}).get("name")
        return "\n".join(f"{who}{' (you)' if who == name else ''}: {text}" for _t, who, text, _s in lines)

    def speaker_names(self) -> set[str]:
        names = {"user", "others", "you", "me", "assistant"}
        for aid, a in self.engine.agents().items():
            names.update(n.lower() for n in [aid, a.get("name", "")] + list(a.get("call_names", [])) if n)
        return names

    def clean(self, reply: str, agent: dict[str, Any]) -> str:
        """The model's reply as something to say out loud, or "" for nothing. Small models often
        answer with a speaker label ("Jeeves: sure") or just a name -- never say those."""
        r = (reply or "").strip()
        r = re.sub(r"^\s*[\"'*(\[]+|[\"'*)\]]+\s*$", "", r).strip()
        names = self.speaker_names()
        for _ in range(2):                                   # "Jeeves (you): ..." / "Jeeves: User: ..."
            m = re.match(r"^\s*([\w .'-]{1,40}?)\s*(\(you\))?\s*:\s*", r)
            if m and (m.group(1).lower() in names or len(m.group(1).split()) <= 2):
                r = r[m.end():].strip()
        if not r or re.search(r"\bpass\b", r, re.I) and len(r.split()) <= 4:
            return ""
        bare = re.sub(r"[^\w ]", "", r).strip().lower()
        if not bare or bare in names or all(w in names for w in bare.split()):
            return ""                                        # just a name (or names)
        if len(bare.split()) < 2 and len(bare) < 4:
            return ""
        return r

    # ---- deciding -----------------------------------------------------------
    def consider(self, source: str) -> None:
        now = time.time()
        for aid, agent in self.agents().items():
            if not self.engine._listens(agent, source):
                continue
            freq = float((agent.get("jump_in") or {}).get("frequency", 0))
            if aid in self.busy or self.engine.speaking:
                continue
            if any(c.agent_id == aid for c in list(self.engine.active.values())):
                continue                              # it's already handling a request
            if now - self.last_spoke.get(aid, 0) < cooldown(freq):
                continue
            if random.random() > consider_chance(freq):
                continue
            self.busy.add(aid)
            self.engine.run_async(self._decide, aid, agent, freq)

    def _decide(self, aid: str, agent: dict[str, Any], freq: float) -> None:
        from ..models.manager import ModelUnavailable
        try:
            convo = self.transcript(aid, agent)
            if not convo or convo.splitlines()[-1].startswith(f"{agent.get('name', aid)} (you):"):
                return                                       # nothing new, or its own line is the latest
            prompt = (f"You are listening to a conversation (most recent line last):\n{convo}\n\n"
                      f"Would you, {agent.get('name', aid)}, say something right now? If yes, reply with exactly "
                      "the words you'd say out loud (one or two short sentences) -- no name, label or quotes in "
                      "front. If not, reply with just PASS.")
            try:
                reply = self.engine.models.respond(agent, prompt, system=style(freq))
            except ModelUnavailable:
                return
            reply = self.clean(reply or "", agent)
            if not reply:
                return
            self._speak(aid, agent, reply, convo)
        except Exception:
            log.exception("jump-in failed")
        finally:
            self.busy.discard(aid)

    def _speak(self, aid: str, agent: dict[str, Any], reply: str, convo: str) -> None:
        from .context import FunctionContext
        from .history import new_entry
        eng = self.engine
        last = convo.splitlines()[-1] if convo else ""
        entry = new_entry(f"[jumped in after] {last}", aid, "jump_in")
        entry["function"] = "jump_in"
        eng.history.add(entry)
        ctx = FunctionContext(eng, aid, agent, entry)
        with eng._lock:
            eng.active[entry["id"]] = ctx
        try:
            ctx.say(reply)
            entry["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = str(exc)
        finally:
            with eng._lock:
                eng.active.pop(entry["id"], None)
            eng.set_indicator(entry["id"], aid, "idle")
            eng.history.finish(entry)
            eng.publish("history", {"entry": entry})
