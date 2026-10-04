"""The Normal Functions from SPEC.md -- what agents actually run.

Defaults (ON/OFF) follow the spec. Keywords are defaults; users can change
every one of them in Settings > Functions.
"""
from __future__ import annotations

import json

from ..util import format_duration, parse_clock, parse_duration
from .base import Arg, FunctionError, full

ONLINE_AGENTS = ["codex", "gemini", "claude", "grok"]
CONTROL_ACTIONS_DOC = (
    'A JSON list of actions run in order on the virtual devices jeeves-keyboard / jeeves-mouse / '
    'jeeves-controller: {"do":"key","key":"KEY_A","state":"down"|"up"|"tap"}, '
    '{"do":"button","button":"BTN_LEFT","state":"down"|"up"|"tap"}, '
    '{"do":"move","x":100,"y":-20,"absolute":false,"duration":0.2}, {"do":"scroll","amount":-3}, '
    '{"do":"type","text":"hello"}, {"do":"wait","seconds":0.5}, {"do":"axis","axis":"LX","value":0.5}, '
    '{"do":"release_all"}.'
)


@full(
    "summary",
    "Keeps (or reads from) a rolling log of everything heard -- your microphone and desktop audio kept "
    "separate, with timestamps -- for the last customizable amount of time (1 hour by default).",
    args=[Arg("action", "string", "What to do", choices=["summarize", "ask", "start", "stop", "status"]),
          Arg("minutes", "number", "How far back to look", required=False, default=None),
          Arg("question", "string", "For 'ask': the question about what was said", required=False, default="")],
    how="While on, the wake word model is off and agent names are found in the continuous transcript instead. "
        "Summaries and questions are answered by the local response model from the log.",
    keywords=["summary", "summarize", "what did", "recap"],
    examples=["Jeeves, summarize the last ten minutes.", "Jeeves, what did he say about the deadline?"],
    default_enabled=False, category="listening", uses=["generate_text"],
)
def summary(ctx, action, minutes=None, question=""):
    s = ctx.engine.summary
    if action == "start":
        ctx.settings.set("summary.enabled", True)
        ctx.engine.apply_settings()
        return ctx.say("Summary log started.")
    if action == "stop":
        ctx.settings.set("summary.enabled", False)
        ctx.engine.apply_settings()
        return ctx.say("Summary log stopped.")
    if action == "status":
        on = ctx.settings.get("summary.enabled")
        return ctx.say(f"Summary log is {'on' if on else 'off'}.")
    if not ctx.settings.get("summary.enabled"):
        raise FunctionError("the summary log is off (turn it on first)")
    window = float(minutes or ctx.settings.get("summary.minutes", 60))
    log_text = s.text(window)
    if not log_text.strip():
        return ctx.say("Nothing has been said in that time.")
    if action == "ask" and question:
        prompt = f"Transcript (timestamps, [mic] = the user, [desktop] = computer audio):\n{log_text}\n\n" \
                 f"Answer from the transcript only: {question}"
    else:
        prompt = f"Summarize this transcript briefly ([mic] = the user, [desktop] = computer audio):\n{log_text}"
    return ctx.say(ctx.call("generate_text", prompt=prompt))


@full(
    "extended_prompt_mode",
    "Keeps listening past pauses until you say 'End extended prompt mode', then handles everything you said "
    "as one request.",
    keywords=["extended prompt mode", "start extended prompt"],
    examples=["Jeeves, extended prompt mode."],
    how="The end phrase is the first keyword with 'end' in front, e.g. 'end extended prompt mode'.",
    default_enabled=True, category="listening",
)
def extended_prompt_mode(ctx):
    ctx.engine.begin_extended(ctx.agent_id)
    ctx.say("Listening until you say: end extended prompt mode.")
    return "extended prompt mode on"


@full(
    "online_prompt",
    "Sends the request to an online AI and reads its answer: GPT through Codex CLI, Gemini through a free "
    "API key, Claude and Grok through the Jeeves browser.",
    args=[Arg("agent", "string", "Which online AI", choices=ONLINE_AGENTS),
          Arg("prompt", "string", "The request to send, cleaned up (without the wake word or the online AI's name)")],
    how="Codex and Gemini answer directly. For Claude and Grok the indicator flashes white: click it, and the "
        "Jeeves browser opens with the request filled in; press send, then the site's copy button -- Jeeves "
        "reads the answer from the clipboard.",
    keywords=["ask chatgpt", "ask gpt", "ask gemini", "ask claude", "ask grok", "online"],
    examples=["Jeeves, ask Gemini what the tallest building in Europe is."],
    default_enabled=False, category="online",
)
def online_prompt(ctx, agent, prompt):
    agent = (agent or ctx.settings.get("functions.online_agent", "codex")).lower()
    if agent in ("gpt", "chatgpt", "openai"):
        agent = "codex"
    if agent not in ONLINE_AGENTS:
        raise FunctionError(f"unknown online AI '{agent}'")
    if ctx.dry_run:
        return f"<{agent} answer to: {prompt}>"
    ctx.state("researching", f"Asking {agent}")
    answer = ctx.engine.online.ask(agent, str(prompt), ctx)
    return ctx.say(answer)


@full(
    "control_mode",
    "Controls the computer with a virtual keyboard, mouse and controller: move the mouse, press and release "
    "mouse buttons and keys, type, scroll.",
    args=[Arg("actions", "list", CONTROL_ACTIONS_DOC, required=False, default=None),
          Arg("instruction", "string", "Plain-language instruction to plan actions for, when actions aren't "
              "given (e.g. 'click the Save button')", required=False, default="")],
    how="The Abort key stops it and releases every key and button it is holding.",
    keywords=["press", "click", "hold", "type", "move the mouse", "scroll"],
    examples=['Jeeves, hold W. -> actions [{"do":"key","key":"KEY_W","state":"down"}]',
              "Jeeves, let go of everything. -> actions [{\"do\":\"release_all\"}]"],
    default_enabled=False, category="control", uses=["find_on_screen", "get_mouse_position", "generate_text"],
)
def control_mode(ctx, actions=None, instruction=""):
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except ValueError as exc:
            raise FunctionError(f"actions aren't valid JSON: {exc}") from exc
    if not actions:
        if not instruction:
            raise FunctionError("nothing to do (no actions or instruction)")
        actions = ctx.engine.plan_control(ctx, str(instruction))
    if ctx.dry_run:
        return {"would_run": actions}
    done = ctx.engine.control.run_actions(actions, ctx)
    return {"ran": done, "held": ctx.engine.control.held()}


@full(
    "local_response",
    "Answers with the local AI model. This is the default when no other function fits.",
    args=[Arg("prompt", "string", "The request, as said")],
    how="Uses the Local AI Model for Full Responses with the agent's default prompt and recent memory.",
    keywords=["tell me", "what", "who", "why", "how", "explain"],
    examples=["Claude, what is the meaning of life?"],
    default_enabled=True, category="responses", uses=["generate_text", "speak", "recent_requests"],
)
def local_response(ctx, prompt):
    if ctx.dry_run:
        return f"<local model answer to: {prompt}>"
    ctx.state("thinking")
    reply = ctx.engine.models.respond(ctx.agent, str(prompt), ctx=ctx, with_memory=True)
    if reply is None:
        raise FunctionError("no local response model is set up (Settings > Models)")
    return ctx.say(reply)


@full(
    "macros",
    "Creates, adjusts, runs or lists Puppetry macros.",
    args=[Arg("action", "string", "What to do", choices=["create", "adjust", "run", "list"]),
          Arg("name", "macro", "Macro name (for adjust/run; for create, a short name)", required=False, default=""),
          Arg("description", "string", "For create/adjust: what the macro should do / what to change",
              required=False, default=""),
          Arg("arguments", "list", "For run: arguments passed to the macro", required=False, default=[])],
    how="Create and adjust use the local response model with the Puppetry Dictionary to write macro code, "
        "check it with Puppetry's own compiler, and save it into Puppetry (enabled, without a key combo). "
        "'the macro I just made' resolves through memory. Run fires the macro through Puppetry's control socket.",
    keywords=["macro", "make a macro", "adjust the macro", "run the macro"],
    examples=["Puppetry, make a macro that spams left click ten times.", "Puppetry, adjust the macro I just made "
              "to click twenty times.", "Jeeves, run the macro 'open all my apps'."],
    default_enabled=False, category="control", uses=["generate_text", "recent_requests", "run_command"],
)
def macros(ctx, action, name="", description="", arguments=None):
    p = ctx.engine.puppetry
    if action == "list":
        names = p.macro_names()
        return ctx.say(("Your macros: " + ", ".join(names)) if names else "You have no macros.")
    if action == "run":
        target = p.resolve_name(name) or name
        if not target:
            raise FunctionError("which macro?")
        if ctx.dry_run:
            return f"<run macro {target} {arguments or []}>"
        p.fire(target, [str(a) for a in (arguments or [])])
        return ctx.say(f"Ran {target}.")
    if action == "create":
        if not description:
            raise FunctionError("what should the macro do?")
        if ctx.dry_run:
            return f"<new macro '{name or description}'>"
        macro = p.create_with_model(ctx, name or "", description)
        return ctx.say(f"Made the macro {macro['name']}.")
    if action == "adjust":
        target = p.resolve_name(name) or p.most_recent_macro(ctx)
        if not target:
            raise FunctionError("which macro should I adjust?")
        if ctx.dry_run:
            return f"<adjusted macro '{target}'>"
        macro = p.adjust_with_model(ctx, target, description)
        return ctx.say(f"Updated {macro['name']}.")
    raise FunctionError(f"unknown macro action '{action}'")


@full(
    "timers",
    "Sets timers and schedules, lists or cancels them. A schedule can also run a request at a time.",
    args=[Arg("action", "string", "What to do", choices=["timer", "schedule", "list", "cancel"]),
          Arg("duration", "duration", "For timer: how long", required=False, default=""),
          Arg("time", "time", "For schedule: when", required=False, default=""),
          Arg("label", "string", "Name for it", required=False, default=""),
          Arg("request", "string", "For schedule: a request to run at that time instead of just alerting",
              required=False, default="")],
    how="Timers show in the bottom-right of the screen (toggle in Settings > Indicators). When one ends Jeeves "
        "plays a sound, speaks and sends a notification.",
    keywords=["timer", "remind me", "schedule", "alarm", "at"],
    examples=["Jeeves, set a timer for ten minutes for the pasta.", "Jeeves, at 7pm, open OBS."],
    default_enabled=True, category="time",
)
def timers(ctx, action, duration="", time="", label="", request=""):
    t = ctx.engine.timers
    if action == "list":
        items = t.list()
        if not items:
            return ctx.say("No timers running.")
        return ctx.say("; ".join(f"{i['label'] or 'timer'}: {format_duration(i['remaining'])} left" for i in items))
    if action == "cancel":
        n = t.cancel(label or None)
        return ctx.say(f"Cancelled {n} timer{'s' if n != 1 else ''}.")
    if action == "timer":
        seconds = parse_duration(duration)
        if ctx.dry_run:
            return f"<timer {seconds:g}s>"
        t.add(seconds, label=label, agent=ctx.agent_id)
        return ctx.say(f"Timer set for {format_duration(seconds)}.")
    if action == "schedule":
        when = parse_clock(time) if time else None
        if when is None:
            raise FunctionError("when?")
        if ctx.dry_run:
            return f"<schedule at {when:%H:%M}>"
        t.add_at(when, label=label, agent=ctx.agent_id, request=request)
        return ctx.say(f"Scheduled for {when:%-I:%M %p}.")
    raise FunctionError(f"unknown timer action '{action}'")


@full(
    "handoff",
    "Passes information to another agent. The sending agent sees the receiving agent's enabled functions and "
    "tells it whatever is relevant. Only used when you clearly ask for it.",
    args=[Arg("to", "agent", "The agent to hand off to"),
          Arg("message", "string", "What to ask or tell the other agent, including the relevant context")],
    how="Which agents an agent may talk to is set per agent. Recent conversation is included automatically.",
    keywords=["ask", "tell", "hand off", "pass to"],
    examples=["Jeeves, with what we've been talking about, ask Claude to give his thoughts on it."],
    blocked=["requests that only mention another agent's name without asking to involve it"],
    default_enabled=True, category="agents", uses=["recent_requests"],
)
def handoff(ctx, to, message):
    if ctx.dry_run:
        return f"<handoff to {to}: {message}>"
    return ctx.engine.handoff(ctx, str(to), str(message))
