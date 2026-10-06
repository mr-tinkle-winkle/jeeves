"""The Normal Functions from SPEC.md -- what agents actually run.

Defaults (ON/OFF) follow the spec. Keywords are defaults; users can change
every one of them in Settings > Functions.
"""
from __future__ import annotations

import json
import re

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
        prompt = (f"Transcript ([mic] = the user, [desktop] = computer audio):\n{log_text}\n\n"
                  "Summarize it so someone who missed it understands: the main topics, what was decided or "
                  "asked, and anything important (names, numbers, plans) -- explained in a few plain sentences, "
                  "not just a list of keywords.")
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
        from .control_phrases import simple_actions
        actions = simple_actions(ctx, str(instruction))       # "click Save", "type hi", "press ctrl+s"
        if actions is None:
            actions = ctx.engine.plan_control(ctx, str(instruction))
        ctx.trace("actions", actions=actions)
    if ctx.dry_run:
        return {"would_run": actions}
    done = ctx.engine.control.run_actions(actions, ctx)
    return {"ran": done, "held": ctx.engine.control.held()}


@full(
    "screen_reading",
    "Looks at the screen: reads the text on it (all of it or one area), answers questions about what's shown "
    "(an error message, a page, a dialog), or says where something is.",
    args=[Arg("question", "string", "What the user wants to know or have read, as said", required=False,
              default=""),
          Arg("region", "region", "Where to look: anywhere, top, bottom, left, right, middle, top-left, ...",
              required=False, default="anywhere"),
          Arg("find", "string", "Something to locate on screen (instead of reading)", required=False, default=""),
          Arg("screen", "string", "Which monitor: current (the one you're working on), all, primary, left, right, "
              "other, or a monitor name. Finding something searches all of them unless one is named.",
              required=False, default="")],
    how="Takes a screenshot, reads it with OCR (Tesseract) and has the local response model answer from the text "
        "it found. Without a local model it reads the text out. Read-only: it never clicks anything.",
    keywords=["on my screen", "on the screen", "read the screen", "read my screen", "what does it say",
              "what does this say", "look at my screen", "can you see my screen", "what am i looking at"],
    examples=["Jeeves, what does this error say?", "Jeeves, read the top of the screen.",
              "Jeeves, where is the save button?"],
    default_enabled=True, category="screen", uses=["read_screen_text", "find_on_screen", "generate_text"],
)
def screen_reading(ctx, question="", region="anywhere", find="", screen=""):
    import re as _re
    if ctx.dry_run:
        return f"<read the screen ({region}) for: {question or find}>"
    q = str(question or "")
    m = _re.search(r"\bwhere(?:'s|\s+is)\s+(?:the\s+)?(.+?)(?:\s+(?:button|icon|link|tab))?"
                   r"(?:\s+on\s+(?:my|the)\s+(?:\w+\s+)?(?:screens?|monitors?))?\s*\??$", q, _re.I)
    if not find and m:
        find = m.group(1)
    ctx.state("thinking", "Looking at the screen")
    if find:
        hit = ctx.call("find_on_screen", target=str(find), region=region, screen=screen or "all")
        where = _describe_position(ctx, hit["x"], hit["y"])
        try:
            ctx.call("mark_screen_position", x=hit["x"], y=hit["y"])     # circle it on screen
        except FunctionError:
            pass
        return ctx.say(f"{hit.get('text') or find} is {where}.")
    text = ctx.call("read_screen_text", region=region, screen=screen or "all")
    ctx.think("Text on screen:\n" + text)
    vision = ctx.engine.models.vision_llm(ctx.agent)
    if vision is not None:                       # a model that can see: show it the screens themselves
        answer = _look_with_vision(ctx, vision, q, text, screen or "all", region)
        if answer:
            return ctx.say(answer)
    if not text.strip():
        return ctx.say("I can't read any text there." if region != "anywhere" else
                       "I can't read any text on the screen right now.")
    from ..daemon import desktop as dk
    try:
        f = dk.focused()
        app = f"\nFocused window: {f.app} -- {f.title}" if f else ""
    except Exception:
        app = ""
    answer = ctx.engine.models.respond(
        ctx.agent,
        f"Text read from {_screen_words(screen)}{'' if region == 'anywhere' else f' ({region} of it)'} with OCR, top "
        f"to bottom (it may contain recognition mistakes and menu clutter):\n{text[:6000]}{app}\n\n"
        f"The user said: {q or 'read the screen'}\n\nAnswer from what's on the screen. If they asked you to read "
        "something, read the relevant part out (skip menus and buttons). If it isn't on screen, say so.",
        ctx=ctx)
    if answer is None:
        answer = text if len(text) < 600 else text[:600].rsplit(" ", 1)[0] + "…"
    return ctx.say(answer)


def _look_with_vision(ctx, vision, question: str, text: str, screen: str, region: str) -> str | None:
    from ..daemon.watcher import grab, image_parts
    from ..models import persona
    try:
        frame = grab(screen)
    except FunctionError as exc:
        ctx.think(f"Couldn't capture the screen: {exc}")
        return None
    try:
        images = frame.get("images") or []
        if not images:
            return None
        names = ", ".join(lbl for lbl, _ in images)
        prompt = (f"These are the user's screens ({names}).{'' if region == 'anywhere' else f' Focus on the {region}.'}"
                  f"\nText found on them by OCR (may have mistakes):\n{text[:3000] or '(none)'}\n\n"
                  f"The user said: {question or 'what is on my screen?'}\n\nAnswer from what you see. Say which "
                  "screen something is on when there's more than one. Plain spoken sentences.")
        messages = [{"role": "user", "content": image_parts(images) + [{"type": "text", "text": prompt}]}]
        if persona.has_persona(ctx.agent):
            messages.insert(0, {"role": "system", "content": persona.identity_block(ctx.agent)})
        return (vision.chat(messages, max_tokens=int(ctx.settings.get("models.local_response.max_tokens", 512)),
                            cancelled=ctx.is_cancelled) or "").strip() or None
    except Exception as exc:  # noqa: BLE001 -- fall back to the OCR answer
        ctx.think(f"Vision model failed: {exc}")
        return None
    finally:
        frame["shot"].unlink(missing_ok=True)


def _screen_words(screen: str) -> str:
    s = (screen or "all").lower()
    if s in ("current", "this", "focused", ""):
        return "the screen the user is working on"
    if s in ("all", "every", "both", "anywhere"):
        return "all of the user's screens"
    return f"the user's {s} screen"


def _describe_position(ctx, x: int, y: int) -> str:
    from ..daemon import desktop as dk
    try:
        outs = dk.outputs()
    except Exception:
        outs = []
    out = next((o for o in outs if o.x <= x < o.x + o.w and o.y <= y < o.y + o.h), None)
    if out is None:
        return f"at {x}, {y}"
    rx, ry = (x - out.x) / max(1, out.w), (y - out.y) / max(1, out.h)
    v = "top" if ry < 0.33 else "bottom" if ry > 0.66 else "middle"
    h = "left" if rx < 0.33 else "right" if rx > 0.66 else ("" if v == "middle" else "middle")
    spot = "the middle" if v == "middle" and not h else f"the {v} {h}".strip() if v != "middle" else f"the {h} middle"
    if len(outs) > 1:
        from .partials.screen import describe_output
        return f"near {spot} of {describe_output(out, outs)}"
    return f"near {spot}"


@full(
    "watch_screen",
    "Watches the screen live (only when asked): comments on what happens and answers questions about it. "
    "Also stops watching.",
    args=[Arg("action", "string", "start or stop", required=False, default="start", choices=["start", "stop"]),
          Arg("screen", "string", "Which monitor: all (default), current, left, right, primary, other or a name",
              required=False, default="all"),
          Arg("talkativeness", "number", "0 = silent (questions only) .. 1 = full commentary", required=False,
              default=None),
          Arg("focus", "string", "Something to watch for and mention, e.g. 'when the download finishes'",
              required=False, default="")],
    how="Captures the screen every couple of seconds, skips unchanged frames, and looks at changed ones with the "
        "vision model (Models > Vision) or, without one, through OCR. Runs until 'stop watching', Close on its "
        "indicator, or Abort.",
    keywords=["watch my screen", "watch the screen", "watch my game", "commentate", "live commentary",
              "stop watching"],
    examples=["Jeeves, watch my screen and tell me when the render finishes.", "Jeeves, commentate my game.",
              "Jeeves, stop watching."],
    default_enabled=True, category="screen", uses=["read_screen_text", "speak"],
)
def watch_screen(ctx, action="start", screen="all", talkativeness=None, focus=""):
    eng = ctx.engine
    current = eng.watchers.get(ctx.agent_id)
    if action == "stop":
        if current is None:
            return ctx.say("I wasn't watching.")
        if ctx.dry_run:
            return "<stop watching>"
        eng.close_request(current.ctx.entry["id"])
        return ctx.say("Stopped watching.")
    if ctx.dry_run:
        return f"<watch {screen} screen; focus: {focus or 'anything interesting'}>"
    if current is not None:
        eng.close_request(current.ctx.entry["id"])
    from ..daemon.watcher import Watcher
    w = Watcher(eng, ctx, screen, None if talkativeness is None else float(talkativeness), str(focus or ""))
    ctx.background = True                       # calling the agent while it watches doesn't pause the watch
    eng.watchers[ctx.agent_id] = w
    ctx.state("watching", focus or "")
    ctx.say(f"Watching{'' if not focus else ' for ' + str(focus)}.")
    try:
        w.run()
    finally:
        if eng.watchers.get(ctx.agent_id) is w:
            del eng.watchers[ctx.agent_id]
    return "stopped watching"


VIDEO_ACTIONS = ["play", "pause", "resume", "stop", "forward", "back", "louder", "quieter", "fullscreen"]


@full(
    "youtube",
    "Finds a YouTube video -- even vaguely described, like 'the newest video from moist critikal' -- and plays it "
    "in the Jeeves video player. Also pauses, resumes, skips, changes volume or closes the video.",
    args=[Arg("action", "string", "What to do", required=False, default="play", choices=VIDEO_ACTIONS),
          Arg("query", "string", "What the video is about / its title (for play)", required=False, default=""),
          Arg("channel", "string", "Whose channel (for play), as said", required=False, default=""),
          Arg("newest", "boolean", "Their newest upload", required=False, default=False),
          Arg("seconds", "number", "For forward/back: how far", required=False, default=None)],
    how="yt-dlp finds the channel or searches YouTube and gets the streams (up to 1080p); the player opens on "
        "screen. Needs yt-dlp.",
    keywords=["youtube", "pull up the video", "play the video", "newest video", "latest video", "pause the video",
              "resume the video", "close the video"],
    examples=["Jeeves, pull up the newest video from moist critikal.", "Jeeves, play lofi hip hop on YouTube.",
              "Jeeves, skip ahead 30 seconds.", "Jeeves, pause the video."],
    default_enabled=True, category="media", uses=["youtube_search"],
)
def youtube(ctx, action="play", query="", channel="", newest=False, seconds=None):
    from .partials import youtube as yt
    eng = ctx.engine
    if action != "play":
        if ctx.dry_run:
            return f"<video {action}>"
        eng.publish("video", {"action": action, "seconds": seconds})
        if action == "stop":
            eng.video_state["playing"] = False
        return ""                                   # the player reacts; nothing to say
    if not query and not channel:
        raise FunctionError("which video?")
    if ctx.dry_run:
        return f"<play {'newest ' if newest else ''}video {query!r} from {channel or 'search'}>"
    ctx.state("researching", "Looking on YouTube")
    v = yt.pick(str(query or ""), str(channel or ""), bool(newest))
    ctx.think(f"Found: {v['title']} — {v['channel']} ({v['url']})", looking_at=v["url"])
    s = yt.streams(v["url"], int(ctx.settings.get("youtube.max_height", 1080)))
    eng.publish("video", {"action": "play", "title": s["title"] or v["title"], "channel": s["channel"] or v["channel"],
                          "video": s["video"], "audio": s["audio"], "page": s["page"], "duration": s["duration"],
                          "agent": ctx.agent_id})
    eng.video_state.update(playing=True, title=s["title"] or v["title"], page=s["page"])
    ctx.entry["sources"] = [{"n": 1, "title": s["title"] or v["title"], "url": s["page"], "text": v["channel"]}]
    return ctx.say(f"Here's {s['title'] or v['title']} from {s['channel'] or v['channel']}.")


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
        raise FunctionError("I didn't get an answer from the local response model. If it keeps happening, "
                            "check Settings > Models")
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
    keywords=["timer", "remind me", "schedule", "alarm"],
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
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            raise FunctionError("how long should the timer be?") from exc
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


ACKS = ["Let me look that up.", "One moment, I'll check.", "Looking into it.", "Let me find out."]


@full(
    "research",
    "Looks things up: searches the web (and the offline Wikipedia, if downloaded), reads the best pages and "
    "answers from them, saying where the answer came from. Use for current events, facts you're unsure of, "
    "prices, releases, scores, anything that needs looking up.",
    args=[Arg("question", "string", "What to find out, as a full question"),
          Arg("depth", "string", "How hard to dig: quick, normal or deep (default: the Research setting)",
              required=False, default="", choices=["", "quick", "normal", "deep"])],
    how="The indicator turns blue (researching); click it to see the pages being read. The local response "
        "model writes the answer from what it read.",
    keywords=["look up", "search for", "research", "google", "find out", "search the web", "what's the latest"],
    examples=["Jeeves, look up when the next Hollow Knight patch comes out.",
              "Jeeves, research the best budget mechanical keyboards."],
    default_enabled=True, category="web", uses=["web_search", "request_website", "wikipedia", "generate_text"],
)
def research(ctx, question, depth=""):
    import random
    from .research import run
    if ctx.dry_run:
        return f"<researched answer to: {question}>"
    question = str(question)
    ctx.say(random.choice(ACKS))                 # looking things up takes a while: say so right away
    try:
        sources, results = run(ctx, question, depth or None)
    except FunctionError as exc:
        ctx.think(f"Research failed: {exc}")
        sources, results = [], []
    if not sources:                              # nothing online: answer from what it knows, and say so
        reply = ctx.engine.models.respond(
            ctx.agent, f"{question}\n\n(You couldn't look this up online just now. Answer from what you know, "
            "and say briefly that you couldn't check it.)", ctx=ctx)
        if reply is None:
            raise FunctionError("I couldn't find anything about that")
        return ctx.say(reply)
    ctx.trace("sources", sources=[{"title": s["title"], "url": s["url"]} for s in sources])
    material = "\n\n".join(f"[{i + 1}] {s['title']} ({s['url']})\n{s['text']}" for i, s in enumerate(sources))
    ctx.state("thinking", "Writing the answer")
    answer = ctx.engine.models.respond(
        ctx.agent,
        f"Question: {question}\n\nSources:\n{material}\n\n"
        "Using only these sources, explain the answer properly -- not a one-line summary. Say what the answer "
        "is, then explain the why or how and the key details a curious person would want (names, numbers, "
        "dates, steps, what it means for them), in about 4 to 8 plain sentences. After each fact put the number "
        "of the source it came from in square brackets, like [2]. Don't add facts the sources don't give: if "
        "they disagree or only partly answer it, say what they do say and what they don't.",
        ctx=ctx)
    if answer is None:                           # no local model: read out the best snippet
        best = next((r for r in results if r.get("snippet")), None)
        answer = f"According to {best['title']}: {best['snippet']} [1]" if best else sources[0]["text"][:400]
    spoken = re.sub(r"\s*\[\d+(?:\s*[,-]\s*\d+)*\]", "", answer).strip()
    show_sources(ctx, question, answer, sources)
    ctx.trace("say", text=spoken)
    ctx.entry["response"] = answer
    ctx.spoke = True
    ctx.state("responding", spoken)
    ctx.engine.speak(ctx, spoken)
    return spoken


def show_sources(ctx, question: str, answer: str, sources: list[dict]) -> None:
    """Answer + clickable sources + the text read from each: the Sources popup (and history)."""
    items = [{"n": i + 1, "title": s["title"], "url": s["url"], "text": s["text"]} for i, s in enumerate(sources)]
    ctx.entry["sources"] = items
    ctx.engine.publish("sources", {"request": ctx.entry["id"], "agent": ctx.agent_id,
                                   "agent_name": ctx.agent.get("name", ctx.agent_id), "question": question,
                                   "answer": answer, "sources": items})


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
