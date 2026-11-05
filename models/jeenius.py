"""The Jeenius scale: how much an agent thinks before it answers (with a thinking model -- Qwen3,
gpt-oss, DeepSeek-R1... -- other models just answer).

  1  Instant              never thinks: the fastest answers
  2  Thinks when needed   answers straight away, and thinks first when the question calls for it
                          (why/how something works, plans, comparisons, maths, estimates, long questions)
                          and for research and screen answers
  3  Instant when it can  thinks first, except for the easy things (greetings, thanks, short commands,
                          small talk)
  4  Always thinks        thinks hard before every answer, and for every step of research too

Per agent (Agents > Jeenius) or for all of them (Settings > Agents > Jeenius)."""
from __future__ import annotations

import re
from typing import Any

LEVELS = {1: "Instant", 2: "Thinks when needed", 3: "Instant when it can", 4: "Always thinks"}
DEFAULT = 2

HARD = re.compile(r"\b(why|how (does|do|did|would|could|can|come)|explain|compare|comparison|differen(ce|t from)|"
                  r"pros and cons|plan|design|strategy|calculate|solve|estimate|chances?|odds|probabilit|prove|"
                  r"debug|what if|should i|which (is|one) (better|best)|step by step|analy[sz]e|reason|"
                  r"figure out|work out|optimi[sz]e|trade-?offs?)\b", re.I)
EASY = re.compile(r"^\W*(hi|hey|hello|yo|thanks|thank you|cheers|ok(ay)?|cool|nice|great|good (morning|night|"
                  r"evening|afternoon)|how are you|what'?s up|never ?mind|stop|yes|no|sure)\b", re.I)


def level(agent: dict[str, Any] | None, settings: Any) -> int:
    own = (agent or {}).get("jeenius")
    try:
        n = int(own) if own not in (None, "", 0) else int(settings.get("models.jeenius", DEFAULT))
    except (TypeError, ValueError):
        n = DEFAULT
    return min(4, max(1, n))


def hard(text: str) -> bool:
    words = text.split()
    return bool(HARD.search(text)) or len(words) > 30 or bool(re.search(r"\d\s*[-+*/^x×÷]\s*\d", text))


def easy(text: str) -> bool:
    return bool(EASY.search(text)) and len(text.split()) <= 8 or len(text.split()) <= 3


def think_for(n: int, kind: str, text: str = "") -> bool | str:
    """Whether to think for one model call. kind: "chat" (a reply), "answer" (from research or the
    screen), "step" (one step of a task: breaking a question down, picking pages...), "intent"."""
    if n <= 1:
        return False
    if n >= 4:
        return "high" if kind in ("chat", "answer") else True
    if kind in ("step", "intent"):
        return False
    if n == 2:
        return kind == "answer" or hard(text)
    return not easy(text)
