"""Training by voice: telling an agent it should, or shouldn't, have responded.

"Jeeves, you shouldn't have responded" -- the last time it woke wasn't for it (a false wake, or talk
to someone else): that request stops if it's still going, its wake-up clip is marked "not a call"
(Training > Recent wake-ups learns from it), its history entry is rated down, and its words are
kept as talk that isn't for the agent (training.not_for_me), so the same sort of thing is ignored.

"Jeeves, you should have responded" -- you called and it didn't wake: what you said just before
this call (the listener keeps the last 20 s of audio) is found, transcribed, kept as a wake-up clip
marked "it was a call" (missed) and as a voice recording (Training > Your voice), and then -- with
training.answer_missed on -- run as the request it was.
"""
from __future__ import annotations

import time
from typing import Any

from ..daemon.audio import FRAME_MS
from .base import FunctionError, full

GAP_FRAMES = 900 // FRAME_MS          # a pause this long inside it still belongs to the same speech
PAD_FRAMES = 300 // FRAME_MS          # kept on each side so the first and last words aren't clipped
MIN_VOICED = 6                        # less speech than this is a click or a breath, not something said
MAX_FRAMES = 15_000 // FRAME_MS
LOOKBACK_SECONDS = 300                # "that" means something from the last few minutes


def last_speech(frames: list[bytes], voiced: list[bool]) -> bytes:
    """The last stretch of speech in the audio (pauses under ~1 s kept inside it), padded a little;
    b"" if there is none."""
    n = min(len(frames), len(voiced))
    frames, voiced = frames[-n:] if n else [], voiced[-n:] if n else []
    end = next((i for i in range(n - 1, -1, -1) if voiced[i]), None)
    if end is None:
        return b""
    start, gap, count = end, 0, 0
    for i in range(end, -1, -1):
        if voiced[i]:
            start, gap, count = i, 0, count + 1
        else:
            gap += 1
            if gap > GAP_FRAMES:
                break
        if end - i >= MAX_FRAMES:
            break
    if count < MIN_VOICED:
        return b""
    return b"".join(frames[max(0, start - PAD_FRAMES): min(n, end + 1 + PAD_FRAMES)])


def _source(ctx: Any) -> str:
    src = str(ctx.entry.get("source") or "")
    return src.split(":", 1)[1] if src.startswith("voice:") else ""


def _previous_voice_request(ctx: Any) -> dict[str, Any] | None:
    """The request a voice call started before this one (this agent's first, else any agent's)."""
    now = time.time()
    entries = [e for e in reversed(ctx.engine.history.entries)
               if e["id"] != ctx.entry["id"] and not e.get("dry_run")
               and str(e.get("source") or "").startswith("voice:")
               and now - float(e.get("time") or 0) < LOOKBACK_SECONDS
               and e.get("function") not in ("should_have_responded", "should_not_have_responded")]
    return next((e for e in entries if e["agent"] == ctx.agent_id), entries[0] if entries else None)


@full(
    "should_not_have_responded",
    "Tells the agent it shouldn't have responded just now: the last time it woke wasn't for it (it misheard "
    "its name, or you were talking to someone else). It stops, and learns from it.",
    keywords=["shouldn't have responded", "should not have responded", "that wasn't for you", "not talking to you",
              "wasn't talking to you", "false alarm", "shouldn't have answered"],
    examples=["Jeeves, you shouldn't have responded.", "Jeeves, that wasn't for you.",
              "Jeeves, I wasn't talking to you."],
    how="The request it stops is the one a voice call started before this one, from the last few minutes. Its "
        "wake-up clip is marked 'not a call' (Training > Recent wake-ups), its history entry is rated down, and "
        "what was said is kept so the same sort of talk is ignored next time.",
    default_enabled=True, category="training", title="Should not have responded",
)
def should_not_have_responded(ctx):
    eng = ctx.engine
    prev = _previous_voice_request(ctx)
    if prev is None:
        raise FunctionError("there's no recent voice request to learn from")
    if ctx.dry_run:
        return {"would_mark": prev["id"], "text": prev.get("text", "")}
    learned: list[str] = []
    if prev["id"] in eng.active:
        eng.close_request(prev["id"])
        learned.append("stopped it")
    clip = next((c for c in eng.clips.list() if c.get("request") == prev["id"]), None)
    if clip is not None and clip.get("verdict") != "bad":
        try:
            eng.clips.mark(clip["id"], False)
            learned.append("marked the wake-up as not a call")
        except ValueError:
            pass
    try:
        eng.history.rate(prev["id"], -1, comment=f"Not meant for {ctx.agent.get('name', ctx.agent_id)}: "
                                                 "it shouldn't have responded")
        learned.append("rated it down")
    except KeyError:
        pass
    text = (prev.get("text") or "").strip()
    if text:
        have = list(eng.settings.get("training.not_for_me", []) or [])
        if text not in have:
            eng.settings.set("training.not_for_me", (have + [text])[-100:])
        learned.append("will ignore talk like it")
    ctx.trace("should_not_have_responded", request=prev["id"], text=text, clip=(clip or {}).get("id"),
              learned=learned)
    ctx.say("Understood. I'll leave that sort of thing alone.")
    return {"request": prev["id"], "text": text, "learned": learned}


@full(
    "should_have_responded",
    "Tells the agent it should have responded: you called it just before this and it didn't wake. What you "
    "said is found, kept for training, and answered now.",
    keywords=["should have responded", "should've responded", "didn't respond", "you missed that",
              "should have answered", "should've answered", "didn't hear me", "i was talking to you"],
    examples=["Jeeves, you should have responded.", "Jeeves, you missed that.",
              "Jeeves, I was talking to you."],
    how="The listener keeps the last 20 seconds of audio: the last speech before this call is the missed one. "
        "It's kept as a wake-up clip marked 'it was a call' and as one of your voice recordings (Training), then "
        "run as a request (training.answer_missed).",
    default_enabled=True, category="training", title="Should have responded",
)
def should_have_responded(ctx):
    eng = ctx.engine
    source = _source(ctx)
    before = getattr(eng, "before_wake", {}) or {}
    if not source or source not in before:              # typed: the latest call on any source
        source = max(before, key=lambda s: before[s][0], default="")
    if not source:
        raise FunctionError("there's no audio from before this call (only spoken calls keep it)")
    _at, frames, voiced = before[source]
    pcm = last_speech(list(frames), list(voiced))
    if not pcm:
        ctx.say("I didn't hear anything before you called me.")
        return {"missed": None}
    if ctx.dry_run:
        return {"would_keep_seconds": round(len(pcm) / 32000, 1)}
    try:
        text = eng.transcribe(pcm, ctx.agent).strip()
    except Exception as exc:  # noqa: BLE001 -- the clip is still worth keeping
        ctx.trace("transcribe_failed", reason=str(exc))
        text = ""
    learned: list[str] = []
    try:
        cid = eng.clips.add(pcm, ctx.agent_id, source)
        eng.clips.update(cid, heard=text, outcome="missed: you said it should have responded")
        eng.clips.update(cid, verdict="good", said="")
        learned.append("kept it as a missed wake-up")
    except OSError:
        cid = None
    if text:
        eng.training.save_recording(text, pcm)
        learned.append("kept it as a voice recording")
    ctx.trace("should_have_responded", source=source, seconds=round(len(pcm) / 32000, 1), heard=text,
              clip=cid, learned=learned)
    if not text:
        ctx.say("I've kept that for training, but I couldn't make out what you said.")
        return {"missed": "", "learned": learned}
    _found, request = eng.strip_name(text, ctx.agent_id)
    request = (request or text).strip(" ,.")
    if not eng.settings.get("training.answer_missed", True) or not request:
        ctx.say("Sorry about that. I've kept it for training.")
        return {"missed": text, "learned": learned}
    ctx.say("Sorry, I missed that.")
    eng.run_async(eng.handle_text, request, ctx.agent_id, source=f"voice:{source}")
    return {"missed": text, "request": request, "learned": learned}
