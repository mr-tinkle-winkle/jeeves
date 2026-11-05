"""Basic control flow and Event Triggers.

Control flow (if / loops / when / conditionals) is written as steps in a full
function's composition -- see ``jeeves/functions/composer.py``. It is listed
here so the Dictionary documents it alongside the partials.
"""
from __future__ import annotations

from ..base import Arg, FunctionError, partial

TRIGGER_EVENTS = ["app_focused", "app_opened", "app_closed", "process_started", "process_stopped",
                  "text_on_screen", "time", "file_changed", "audio_keyword"]


@partial(
    "control_flow",
    "Conditionals and loops for compositions: if / else, repeat N times, while, for each, when (event).",
    how='Used as steps in a full function, e.g. {"if": "app == \\"obs\\"", "then": [...], "else": [...]}, '
        '{"repeat": 3, "do": [...]}, {"while": "count < 5", "do": [...]}, {"for_each": "${items}", "do": [...]}, '
        '{"when": {"event": "app_focused", "app": "steam"}, "do": [...]}. Not called directly.',
    category="flow",
)
def control_flow(ctx, **_):
    raise FunctionError("control flow is written as steps in a composition, not called directly")


@partial(
    "event_trigger",
    "Sets up something to happen later when an event occurs: an app is focused/opened/closed, a process "
    "starts/stops, text appears on screen, a time is reached, a file changes, or a keyword is heard.",
    args=[Arg("event", "string", "The kind of event", choices=TRIGGER_EVENTS),
          Arg("target", "string", "App name, process name, text, time ('7:30 pm'), file path, or keyword"),
          Arg("request", "string", "What to do when it happens, as a request to this agent, "
              "e.g. 'start recording'"),
          Arg("source", "string", "For audio_keyword: which audio to listen to", required=False,
              default="microphone", choices=["microphone", "desktop", "both"]),
          Arg("repeat", "boolean", "Keep the trigger after it fires", required=False, default=False)],
    returns="the trigger id", category="flow",
)
def event_trigger(ctx, event, target, request, source="microphone", repeat=False):
    spec = {"event": event, "target": target, "source": source, "repeat": bool(repeat)}
    return ctx.engine.triggers.add(spec, agent=ctx.agent_id, request=str(request))
