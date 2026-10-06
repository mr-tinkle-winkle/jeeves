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
    how="Takes a screenshot and reads it with OCR (Tesseract), finds the part the question is about, zooms in "
        "and reads just that again, then has the local response model (or the vision model, shown the zoomed-in "
        "part) answer from it. Read-only: it never clicks anything.",
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
    return _read_and_answer(ctx, q, region, screen or "all")


MAX_PICKED_BLOCKS = 8


def _read_and_answer(ctx, question: str, region: str, screen: str) -> str:
    """Read the screen the way a person would: glance over it, find the part that matters, look
    closely at just that, then answer.

    1. One screenshot, OCR'd whole, grouped into blocks (a chat message, a paragraph, a dialog...).
    2. The model gets an outline of the blocks -- where each is, whether it's in the window you're
       using -- and picks the ones your question is about, in the order they should be read.
    3. Each picked block is cropped, enlarged and read again on its own: far fewer OCR mistakes, and
       its lines come out in order. A vision model gets that crop at full resolution as well.
    4. The model answers from the clean text. The raw OCR is never read out."""
    from ..daemon import desktop as dk
    from .partials import screen as sc
    shot = sc.screenshot()
    try:
        width, height = sc._image_size(shot)
        mp = sc.Mapper((width, height))
        rect = sc._rect_for(region, width, height, mp, screen)
        blocks = sc.layout_blocks([w for w in sc.ocr_words(shot) if sc._inside(w, rect)])
        try:
            outs = dk.outputs()
        except Exception:  # noqa: BLE001
            outs = []
        if not outs:                             # layout unknown: the screenshot is the one screen
            from types import SimpleNamespace
            bx, by, bw, bh = mp.box
            outs = [SimpleNamespace(name="screen", x=bx, y=by, w=bw, h=bh, primary=True)]
        try:
            f = dk.focused()
        except Exception:  # noqa: BLE001
            f = None
        frect = mp.rect_to_image((f.x, f.y, f.w, f.h)) if f and f.w and f.h else None
        wins = _windows_in_image(dk, mp, f)
        for b in blocks:
            b["where"] = _describe_position(ctx, *mp.point(b["x"] + b["w"] / 2, b["y"] + b["h"] / 2), outs=outs)
            b["focused"] = bool(frect and sc._inside(b, frect))
            b["app"] = next((w["name"] for w in wins if sc._inside(b, w["rect"])), "")
            if b["app"]:
                b["where"] += f", in {b['app']}"
        app = f"The window they're using: {f.app} -- {f.title}\n" if f else ""
        app += _app_hints({b["app"] for b in blocks} | {f.app if f else ""} | {w["name"] for w in wins})
        vision = ctx.engine.models.vision_llm(ctx.agent)
        if not blocks and vision is None:
            return ctx.say("I can't read any text there." if region != "anywhere" else
                           "I can't read any text on the screen right now.")
        picked = _with_neighbours(blocks, _pick_blocks(ctx, question, blocks, app)) if blocks else []
        ctx.state("thinking", "Reading it closely")
        pieces = []
        for b in picked:
            clean = sc.reread(shot, (b["x"], b["y"], b["w"], b["h"]), b["h"] / max(1, b["lines"]))
            # keep the first reading if zooming in lost most of it
            pieces.append((b, clean if len(clean) >= 0.6 * len(b["text"]) else b["text"]))
        ctx.think("Read closely:\n" + "\n\n".join(f"[{b['where']}]\n{t}" for b, t in pieces))
        text = "\n\n".join(f"[{b['where']}{', in the window they are using' if b['focused'] else ''}]\n{t}"
                            for b, t in pieces)
        q = question or "read what's on my screen"
        answer = None
        if vision is not None:
            area = sc.union([(b["x"], b["y"], b["w"], b["h"]) for b in picked]) if picked else rect
            answer = _look_with_vision(ctx, vision, q, text, sc.crop_jpeg(shot, area), app)
        if answer is None and text:
            answer = ctx.engine.models.respond(
                ctx.agent,
                f"The rest of the screen at a glance (rough, only to understand the layout):\n{_glance(blocks, picked)}"
                f"\n\n{app}The part of the screen that matters, read closely (OCR: fix obvious misreadings silently, "
                f"skip anything garbled -- never read it out):\n{text[:5000]}\n\n"
                f"The user said: {q}\n\nWork out what the screen shows -- which app, what kind of list or view, what "
                "belongs to what -- then answer that. To read something out, read it naturally and in order, "
                "skipping usernames, timestamps, buttons and menus unless they matter; summarize when that serves "
                "them better. If it isn't there, say so. Don't describe anything they didn't ask about.",
                ctx=ctx, temperature=0.3)
        if answer:
            return ctx.say(answer)
        if not text:
            return ctx.say("I couldn't find anything about that on the screen.")
        ctx.show(text)                           # the clean text is on screen; never read raw OCR aloud
        if len(text) < 160 and len(pieces) == 1:
            return ctx.say(pieces[0][1])
        return ctx.say("I read it, but couldn't get an answer together from the model. I've put the text up "
                       "for you.")
    finally:
        shot.unlink(missing_ok=True)


APP_HINTS = {
    "discord": "In Discord: the server's channels are in the left sidebar; the people in a voice channel are listed "
               "right under that voice channel's name (indented, with avatars); 'Voice Connected' near the bottom "
               "left shows the call the user is in, and a call opened in the main area shows its participants as "
               "tiles with names. The member list on the right groups people under role headers like "
               "'Developer — 1' (role name — how many online), with each member's name listed right below its "
               "header. Messages show the author's name, then the time, then the message.",
    "steam": "In Steam: the library list is on the left; friends are in the Friends & Chat window, grouped as In-Game, "
             "Online and Offline.",
    "spotify": "In Spotify: what's playing is in the bar at the bottom (title, then artist).",
    "firefox": "A web browser: the page's own content is in the middle; tabs are along the top.",
    "chrom": "A web browser: the page's own content is in the middle; tabs are along the top.",
}


def _app_hints(apps: set[str]) -> str:
    seen, out = set(), []
    for a in apps:
        for key, hint in APP_HINTS.items():
            if a and key in a.lower() and hint not in seen:
                seen.add(hint)
                out.append(hint)
    return ("How these apps are laid out:\n" + "\n".join(out) + "\n") if out else ""


def _windows_in_image(dk, mp, focused) -> list[dict]:
    """Visible windows as image rectangles with readable names, the focused one first, then smallest
    first (a small window on top of a big one is more likely what's showing there)."""
    try:
        ws = [w for w in dk.windows() if w.w > 40 and w.h > 40]
    except Exception:  # noqa: BLE001
        ws = [focused] if focused else []
    names = {"discord": "Discord", "vesktop": "Discord", "webcord": "Discord", "steam": "Steam",
             "spotify": "Spotify", "firefox": "Firefox", "chromium": "Chromium", "google-chrome": "Chrome",
             "brave-browser": "Brave", "code": "VS Code", "konsole": "the terminal", "alacritty": "the terminal",
             "kitty": "the terminal", "org.kde.dolphin": "the file manager", "obs": "OBS"}
    out = []
    for w in sorted(ws, key=lambda w: (not w.focused, w.w * w.h)):
        app = (w.app or "").lower()
        name = next((v for k, v in names.items() if k in app), "") or (w.app or "").split(".")[-1]
        if "discord" in (w.title or "").lower():
            name = "Discord"
        out.append({"rect": mp.rect_to_image((w.x, w.y, w.w, w.h)), "name": name or "a window"})
    return out


HEADER = re.compile(r"^[^\n]{1,40}\s[—–-]\s*\d+$|^[A-Z][A-Z0-9 &'-]{2,30}$")


def _with_neighbours(blocks: list[dict], picked: list[dict], per_block: int = 6) -> list[dict]:
    """The picked blocks plus what's listed right under them in the same column: picking a header
    ("Developer — 1") brings the names under it; picking a list entry brings the rest of its group."""
    out: list[dict] = []
    for b in picked:
        if b in out:
            continue
        out.append(b)
        cur, added = b, 0
        while added < per_block:
            lh = max(8.0, cur["h"] / max(1, cur["lines"]))
            below = [c for c in blocks if c not in out and c.get("app") == b.get("app") and
                     0 <= c["y"] - (cur["y"] + cur["h"]) <= 2.6 * lh and abs(c["x"] - cur["x"]) <= 4 * lh]
            if not below:
                break
            nxt = min(below, key=lambda c: c["y"])
            if HEADER.match(nxt["text"].split("\n")[0].strip()):
                break                                      # the next group starts
            out.append(nxt)
            cur, added = nxt, added + 1
    return out[:MAX_PICKED_BLOCKS + 6]


def _glance(blocks: list[dict], picked: list[dict]) -> str:
    lines = [f"- ({b['where']}) {b['text'][:60].replace(chr(10), ' / ')}" for b in blocks[:60] if b not in picked]
    return "\n".join(lines)[:1800].rsplit("\n", 1)[0] if len("\n".join(lines)) > 1800 else \
        ("\n".join(lines) or "(nothing else)")


def _pick_blocks(ctx, question: str, blocks: list[dict], app: str) -> list[dict]:
    """The blocks the question is about, in reading order -- chosen by the model from an outline."""
    import re as _re
    from .research import keywords
    lines, total = [], 0
    for i, b in enumerate(blocks[:80]):                  # within ~7000 characters: the model's context is small
        ln = f"[{i + 1}] ({b['where']}{', *' if b['focused'] else ''}) {b['text'][:140].replace(chr(10), ' / ')}"
        if total + len(ln) > 7000:
            break
        lines.append(ln)
        total += len(ln) + 1
    outline = "\n".join(lines)
    reply = ctx.engine.models.respond(
        ctx.agent,
        f"Text found on the user's screen, as numbered blocks (where each is; * = in the window they're using):\n"
        f"{outline}\n\n{app}The user asked: {question or 'read my screen'}\n\nWhich blocks are needed to answer? "
        "List their numbers in the order they should be read (a conversation oldest to newest, unless they asked "
        "about the latest message). When the answer is a list under a header (people under a role, users in a voice "
        "channel), pick the header AND the entries under it. Leave out anything unrelated -- menus and sidebars "
        "too, unless the question is about what's in them. Reply with the numbers only, "
        "like: 4, 7, 2. Reply ALL to read everything in the window they're using, or NONE if nothing fits.",
        ctx=None, raw=True, temperature=0.0, max_tokens=60)
    if reply is not None:
        ctx.think(f"Looking at blocks: {reply.strip()[:80]}")
        if _re.search(r"\bNONE\b", reply, _re.I) and not _re.search(r"\d", reply):
            return []
        if _re.search(r"\bALL\b", reply, _re.I) and not _re.search(r"\d", reply):
            inside = [b for b in blocks if b["focused"]] or blocks
            keep = sorted(inside, key=lambda b: -len(b["text"]))[:MAX_PICKED_BLOCKS]
            return [b for b in inside if b in keep]
        nums = [int(n) for n in _re.findall(r"\d+", reply)]
        picked = []
        for n in nums:
            if 1 <= n <= len(blocks) and blocks[n - 1] not in picked:
                picked.append(blocks[n - 1])
        if picked:
            return picked[:MAX_PICKED_BLOCKS]
    # no model (or no usable reply): blocks sharing words with the question, else the window in use
    kws = set(keywords(question))
    scored = sorted(blocks, key=lambda b: (-len(kws & set(keywords(b["text"]))), not b["focused"], -len(b["text"])))
    best = [b for b in scored if kws & set(keywords(b["text"]))] or [b for b in blocks if b["focused"]] or blocks
    keep = best[:MAX_PICKED_BLOCKS]
    return [b for b in blocks if b in keep]              # in screen order


def _look_with_vision(ctx, vision, question: str, text: str, image: bytes | None, app: str) -> str | None:
    """A model that can see: the enlarged crop of the part that matters, plus its clean text."""
    from ..daemon.watcher import image_parts
    from ..models import persona
    if not image:
        return None
    try:
        prompt = (f"This is the part of the user's screen that matters, at full resolution.\n{app}"
                  f"Text read from it (may have small mistakes):\n{text[:3000] or '(none)'}\n\n"
                  f"The user said: {question}\n\nAnswer from what you see. If they asked you to read something, "
                  "read it naturally, skipping usernames, timestamps and buttons unless they matter. Plain spoken "
                  "sentences.")
        messages = [{"role": "user", "content": image_parts([("crop", image)]) + [{"type": "text", "text": prompt}]}]
        if persona.has_persona(ctx.agent):
            messages.insert(0, {"role": "system", "content": persona.identity_block(ctx.agent)})
        return (vision.chat(messages, max_tokens=int(ctx.settings.get("models.local_response.max_tokens", 512)),
                            temperature=0.3, cancelled=ctx.is_cancelled) or "").strip() or None
    except Exception as exc:  # noqa: BLE001 -- fall back to the text answer
        ctx.think(f"Vision model failed: {exc}")
        return None


def _screen_words(screen: str) -> str:
    s = (screen or "all").lower()
    if s in ("current", "this", "focused", ""):
        return "the screen the user is working on"
    if s in ("all", "every", "both", "anywhere"):
        return "all of the user's screens"
    return f"the user's {s} screen"


def _describe_position(ctx, x: int, y: int, outs: list | None = None) -> str:
    from ..daemon import desktop as dk
    if outs is None:
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


VIDEO_ACTIONS = ["play", "pause", "resume", "stop", "forward", "back", "louder", "quieter", "fullscreen", "faster",
                 "slower", "next_chapter", "previous_chapter"]


@full(
    "youtube",
    "Finds a YouTube video -- even vaguely described, like 'the newest video from moist critikal' -- and plays it "
    "in the Jeeves video player. Also pauses, resumes, skips, changes volume or closes the video.",
    args=[Arg("action", "string", "What to do", required=False, default="play", choices=VIDEO_ACTIONS),
          Arg("query", "string", "What the video is about / its title (for play)", required=False, default=""),
          Arg("channel", "string", "Whose channel (for play), as said", required=False, default=""),
          Arg("newest", "boolean", "Their newest upload", required=False, default=False),
          Arg("seconds", "number", "For forward/back: how far", required=False, default=None)],
    how="yt-dlp finds the exact channel (its @handle, else YouTube's channel search) and its newest uploads, or "
        "searches YouTube -- inside the channel when one is named -- and picks the video whose title best fits the "
        "description. The player opens fullscreen with speed, quality, chapters and frame stepping. Needs yt-dlp.",
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
                          "height": s.get("height"), "heights": s.get("heights"), "fps": s.get("fps"),
                          "chapters": s.get("chapters"), "agent": ctx.agent_id})
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
    from .research import best_of, fit, run
    if ctx.dry_run:
        return f"<researched answer to: {question}>"
    question = str(question)
    ctx.say(random.choice(ACKS))                 # looking things up takes a while: say so right away
    original = ctx.engine.intent.strip_address(ctx.agent, ctx.entry.get("text") or "") or question
    try:
        sources, _, notes = run(ctx, question, depth or None)
    except FunctionError as exc:
        ctx.think(f"Research failed: {exc}")
        sources, _, notes = [], [], []
    learned = ("Background you looked up first:\n" + "\n".join(f"- {n}" for n in notes) + "\n\n") if notes else ""
    if not sources:                              # nothing online: answer from what it knows, and say so
        reply = ctx.engine.models.respond(
            ctx.agent, f"{learned}{original}\n\n(You couldn't look this up online just now. If you genuinely know the "
            "answer, give it and say briefly that you couldn't check it. If it's about something specific you "
            "don't clearly know -- a particular game's moves or items, a small community, a recent event -- do NOT "
            "guess or make something up: say you couldn't look it up and don't know.)", ctx=ctx, temperature=0.3)
        if reply is None:
            raise FunctionError("I couldn't look that up online, and I don't know it myself")
        return ctx.say(reply)
    sources = best_of(sources, f"{original} {question}")
    ctx.trace("sources", sources=[{"title": s["title"], "url": s["url"]} for s in sources])
    material = fit(sources, 9000)
    ctx.state("thinking", "Writing the answer")
    asked = original if original.strip().lower() == question.strip().lower() else f"{original}\n(Meaning: {question})"
    answer = ctx.engine.models.respond(
        ctx.agent,
        f"Sources:\n{material}\n\n{learned}The user asked: {asked}\n\n"
        "Answer exactly that from these sources, nothing more. Skip any source that's about something else (another "
        "game, item or person); if none of them answer it, say you couldn't find it. "
        f"{_answer_shape(original + ' ' + question)} No history, background or general information unless they "
        "asked for it. After each fact put the number of its source in square brackets, like [2]. Never add "
        "anything the sources don't say; if they only partly answer it, say in one sentence what's missing.",
        ctx=ctx, temperature=0.3)
    if answer is None:
        # no answer from the model: never read a raw snippet out as if it were one (that's how a line
        # about something else entirely got said as the answer) -- point at the sources instead
        show_sources(ctx, question, "", sources)
        return ctx.say(f"I found {len(sources)} page{'s' if len(sources) != 1 else ''} about it but couldn't put "
                       "an answer together. They're in the sources window.")
    spoken = re.sub(r"\s*\[\d+(?:\s*[,-]\s*\d+)*\]", "", answer).strip()
    show_sources(ctx, question, answer, sources)
    ctx.trace("say", text=spoken)
    ctx.entry["response"] = answer
    ctx.spoke = True
    ctx.state("responding", spoken)
    ctx.engine.speak(ctx, spoken)
    return spoken


def _answer_shape(question: str) -> str:
    from .research import kind_of
    if kind_of(question) == "howto":
        return ("They want to know how to do it: give the steps in order, as short plain sentences (2 to 5), with "
                "the exact keys, timings or requirements the sources give.")
    return "Give the answer in the first sentence, then at most two sentences of the detail that matters most."


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
