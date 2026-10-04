"""Memory: recent requests, long-term (RAM) and permanent (disk) memory."""
from __future__ import annotations

from ..base import Arg, partial


@partial("recent_requests", "Gets the most recent requests and what was done for them (default: the last 3, "
         "customizable), e.g. to adjust 'the macro I just made'.",
         args=[Arg("count", "integer", "How many", required=False, default=None),
               Arg("agent", "agent", "Only this agent's requests", required=False, default=None)],
         returns="list of {text, agent, function, args, result}", category="memory", dry_run_safe=True)
def recent_requests(ctx, count=None, agent=None):
    n = int(count or ctx.settings.get("memory.recent_count", 3))
    return ctx.engine.history.recent(n, agent=agent, exclude=ctx.request.get("id"))


@partial("remember", "Commits something to memory. 'long_term' keeps it while Jeeves runs (RAM); "
         "'permanent' saves it to disk forever.",
         args=[Arg("text", "string", "What to remember"),
               Arg("duration", "string", "How long", required=False, default="long_term",
                   choices=["long_term", "permanent"])],
         keywords=["remember for a while", "commit to long term memory", "remember forever",
                   "commit to permanent memory"],
         category="memory")
def remember(ctx, text, duration="long_term"):
    ctx.engine.memory.add(str(text), permanent=(duration == "permanent"), agent=ctx.agent_id)
    return f"remembered ({duration.replace('_', ' ')})"


@partial("recall", "Searches long-term and permanent memory.",
         args=[Arg("query", "string", "What to look for (empty = everything)", required=False, default="")],
         returns="list of remembered notes", category="memory", dry_run_safe=True)
def recall(ctx, query=""):
    return [m["text"] for m in ctx.engine.memory.search(str(query))]


@partial("forget", "Removes remembered notes that match.",
         args=[Arg("query", "string", "Which notes")], returns="how many were removed", category="memory")
def forget(ctx, query):
    return ctx.engine.memory.forget(str(query))
