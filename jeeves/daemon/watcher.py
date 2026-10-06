"""Watch the screen: live commentary and questions about what's on it.

Started (and stopped) only on request ("Jeeves, watch my screen"). While it runs:

* every ``watch.interval`` seconds (2 s by default) the chosen monitor is captured,
  shrunk and compared with the previous frame; unchanged frames cost nothing more,
  and while nothing changes the interval backs off a little (up to 2x);
* a changed frame is looked at -- by a vision model when one is set up (Models >
  Vision: it gets the picture), otherwise through OCR text and the focused window;
* the agent keeps a short timeline of what it saw and comments out loud when it
  has something worth saying. Talkativeness works like Jump in: 1 = commentator,
  0.5 = interesting moments, 0.1 = only important things (and what you asked it to
  watch for), 0 = silent (questions only);
* while watching, the agent's other answers know what's on screen (and see the
  latest frame with a vision model), so "what was that?" just works.

The watch is a request like any other: its indicator shows it, right-click >
Suspend pauses it, Close (or "stop watching", or Abort) ends it.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from collections import deque
from typing import Any

from ..functions.base import Cancelled, FunctionError

log = logging.getLogger("jeeves.watcher")
MAX_WIDTH = 1024
THUMB_W, THUMB_H = 192, 108
CHANGED = 0.0007          # ~15 of 20736 thumbnail pixels: even two changed digits, a popup, a new scene


def grab(screen: str) -> dict[str, Any]:
    """One frame of the chosen monitor: {jpeg, thumb (32x18 grey), text (OCR, lazily), size}."""
    from ..functions.partials.screen import Mapper, _image_size, _rect_for, screenshot
    shot = screenshot()
    try:
        w, h = _image_size(shot)
        mp = Mapper((w, h))
        x, y, rw, rh = _rect_for("anywhere", w, h, mp, screen)
        frame: dict[str, Any] = {"path": None, "rect": (x, y, rw, rh), "mapper": mp}
        try:
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
            from PySide6.QtGui import QImage
            img = QImage(str(shot))
            if not img.isNull():
                img = img.copy(QRect(x, y, rw, rh))
                if img.width() > MAX_WIDTH:
                    img = img.scaledToWidth(MAX_WIDTH, Qt.SmoothTransformation)
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.WriteOnly)
                img.save(buf, "JPG", 70)
                frame["jpeg"] = bytes(ba)
                g = img.scaled(THUMB_W, THUMB_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation) \
                    .convertToFormat(QImage.Format_Grayscale8)
                bpl = g.bytesPerLine()
                raw = bytes(g.constBits())[: bpl * THUMB_H]
                frame["thumb"] = b"".join(raw[r * bpl: r * bpl + THUMB_W] for r in range(THUMB_H))
        except Exception:  # noqa: BLE001 -- no Qt: OCR-only watching
            pass
        frame["shot"] = shot
        return frame
    except Exception:
        shot.unlink(missing_ok=True)
        raise


def difference(a: bytes | None, b: bytes | None) -> float:
    """Share of thumbnail pixels that clearly changed (0 = same .. 1 = all). A line of text changing
    on a big screen moves only a few dozen of them, so even that counts."""
    if not a or not b or len(a) != len(b):
        return 1.0
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 12) / len(a)


def ocr_text(frame: dict[str, Any]) -> str:
    from ..functions.partials.screen import _group_lines, _inside, ocr_words
    try:
        words = ocr_words(frame["shot"])
    except FunctionError:
        return ""
    return "\n".join(l["text"] for l in _group_lines([w for w in words if _inside(w, frame["rect"])]))[:4000]


class Watcher:
    def __init__(self, engine: Any, ctx: Any, screen: str = "current", talkativeness: float | None = None,
                 focus: str = "") -> None:
        self.engine, self.ctx = engine, ctx
        self.screen = screen or "current"
        s = engine.settings
        self.talk = float(s.get("watch.talkativeness", 0.5) if talkativeness is None else talkativeness)
        self.focus = focus.strip()
        self.interval = max(0.5, float(s.get("watch.interval", 2.0)))
        self.timeline: deque[tuple[float, str]] = deque(maxlen=20)
        self.said: deque[tuple[float, str]] = deque(maxlen=8)
        self.latest_jpeg: bytes | None = None
        self.latest_text = ""
        self.last_spoke = 0.0
        self.started = time.time()

    # ---- what other answers get --------------------------------------------
    def context_note(self) -> str:
        if not self.timeline:
            return ""
        now = time.time()
        seen = "\n".join(f"- {int(now - t)}s ago: {d}" for t, d in list(self.timeline)[-8:])
        text = f"\nText on screen right now:\n{self.latest_text[:1500]}" if self.latest_text else ""
        return f"You are watching the user's screen live. What you've seen (newest last):\n{seen}{text}"

    # ---- the loop ---------------------------------------------------------
    def run(self) -> None:
        ctx, eng = self.ctx, self.engine
        max_minutes = float(eng.settings.get("watch.max_minutes", 120))
        prev_thumb: bytes | None = None
        last_look = 0.0
        quiet = 0
        vision = eng.models.vision_llm(ctx.agent)
        ctx.think(f"Watching {self.screen} screen with {'a vision model' if vision else 'OCR (no vision model set up)'}")
        while not ctx.is_cancelled():
            ctx.gate()                                     # right-click > Suspend pauses watching
            if time.time() - self.started > max_minutes * 60:
                ctx.say("I'll stop watching now.")
                break
            t0 = time.time()
            try:
                frame = grab(self.screen)
            except FunctionError as exc:
                ctx.think(f"Couldn't capture the screen: {exc}")
                ctx.wait(5)
                continue
            try:
                change = difference(prev_thumb, frame.get("thumb"))
                stale = time.time() - last_look > 30
                if change > CHANGED or stale or prev_thumb is None:
                    prev_thumb = frame.get("thumb")
                    last_look = time.time()
                    quiet = 0
                    self._look(frame, vision, change)
                else:
                    quiet = min(quiet + 1, 2)
            finally:
                frame["shot"].unlink(missing_ok=True)
            # nothing changing: look a little less often (up to 2x the interval)
            ctx.wait(max(0.2, self.interval * (1 + quiet / 2) - (time.time() - t0)))

    def _look(self, frame: dict[str, Any], vision: Any, change: float) -> None:
        ctx, eng = self.ctx, self.engine
        self.latest_jpeg = frame.get("jpeg")
        if vision is None or not self.latest_jpeg:
            self.latest_text = ocr_text(frame)
        name = ctx.agent.get("name", "the assistant")
        recent = "\n".join(f"- {d}" for _t, d in list(self.timeline)[-5:]) or "(just started)"
        said = "\n".join(f"- {s}" for _t, s in list(self.said)[-4:]) or "(nothing yet)"
        from .jumpin import cooldown, style
        may_speak = self.talk > 0 and time.time() - self.last_spoke >= cooldown(self.talk) * 0.35
        focus = f"The user asked you to watch for: {self.focus}. Always mention it when it happens.\n" \
            if self.focus else ""
        task = (f"You are {name}, watching the user's screen live.\n{focus}What you saw before:\n{recent}\n"
                f"What you said recently:\n{said}\n\nReply in exactly two lines:\nSEEN: <one short line on "
                "what's on screen now and what changed>\nSAY: <a short comment out loud, in character, or PASS>"
                + ("" if may_speak else "\n(Say PASS this time unless it's what you were asked to watch for.)"))
        try:
            if vision is not None and self.latest_jpeg:
                b64 = base64.b64encode(self.latest_jpeg).decode()
                messages = [{"role": "system", "content": style(self.talk)},
                            {"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                {"type": "text", "text": task}]}]
                if ctx.agent.get("prompt"):
                    from ..models import persona
                    messages.insert(0, {"role": "system", "content": persona.identity_block(ctx.agent)})
                reply = vision.chat(messages, max_tokens=160, cancelled=ctx.is_cancelled)
            else:
                window = ""
                try:
                    from . import desktop as dk
                    f = dk.focused()
                    window = f"Focused window: {f.app} -- {f.title}\n" if f else ""
                except Exception:  # noqa: BLE001
                    pass
                reply = eng.models.respond(
                    ctx.agent, f"{window}Text on screen (OCR, may have errors):\n{self.latest_text or '(none)'}\n\n"
                    f"{task}", system=style(self.talk), ctx=None)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- keep watching
            ctx.think(f"Couldn't look: {exc}")
            return
        seen, say = parse(reply or "")
        if seen:
            self.timeline.append((time.time(), seen))
            ctx.think(f"[{time.strftime('%H:%M:%S')}] {seen}")
            eng.set_indicator(ctx.entry["id"], ctx.agent_id, "watching", seen[:80])
        if say and (may_speak or self.focus):
            self.said.append((time.time(), say))
            self.last_spoke = time.time()
            ctx.say(say)
            eng.set_indicator(ctx.entry["id"], ctx.agent_id, "watching", seen[:80] if seen else "")


def parse(reply: str) -> tuple[str, str]:
    seen = re.search(r"SEEN:\s*(.+)", reply)
    say = re.search(r"SAY:\s*(.+)", reply)
    s = say.group(1).strip().strip('"') if say else ""
    if not s or re.match(r"^\(?pass\b", s, re.I):
        s = ""
    return (seen.group(1).strip() if seen else reply.strip().splitlines()[0][:160] if reply.strip() else ""), s
