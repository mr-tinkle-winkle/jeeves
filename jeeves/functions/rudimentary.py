"""Rudimentary agents (Agents > Abilities > Rudimentary agent): an agent with exactly one function.

It doesn't have to work out WHICH function you want, only HOW you want that one done, so it's fast:

  - a function with nothing to fill in (say, "Pause music") runs as soon as its name is heard -- no
    model at all;
  - one with arguments (Timers: how long, what for) has them filled in by the intent model from a
    prompt about that one function alone -- a few lines instead of the whole Dictionary -- without
    thinking, and with a short answer; unmistakable requests skip even that (the same rules and
    keyword matching full agents use);
  - if something it needs is missing ("Timer" with no length) it asks for just that.

It never falls back to another function (no chat replies, no research): anything it's told is taken
as a way of running its one function.

The function can be any full function (built-in or your own) or one of the agent's own Commands."""
from __future__ import annotations

from typing import Any

ARGS_SYSTEM = """You fill in the arguments of the one function {agent} (a voice assistant) runs. Every request is for
this function; work out how the user wants it done. For an argument with listed values, use one of them;
leave out arguments the request doesn't need. Only if it can't run without a missing detail, reply
{{"question": "<ask for just that detail>"}}.
Reply with JSON only: {{"args": {{...}}}}

{dictionary}
"""


def function_name(agent: dict[str, Any] | None) -> str | None:
    """The agent's one function, when it's a rudimentary agent."""
    r = (agent or {}).get("rudimentary") or {}
    if isinstance(r, dict) and r.get("enabled") and r.get("function"):
        return str(r["function"])
    return None
