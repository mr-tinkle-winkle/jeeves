"""Control Mode without the model for the common, unambiguous instructions:

    click Save / double-click the Downloads folder / right-click here
    type hello world          press ctrl+s / press enter / hit alt f4
    scroll down (a lot)       hold w / let go of everything
    move the mouse to the top left / move the mouse to Settings

Small local models plan these badly (or answer in chat instead), and they don't
need planning: find the text on screen, move there, click. Anything else goes to
the model (``Engine.plan_control``).
"""
from __future__ import annotations

import re
from typing import Any

from .base import FunctionError

KEY_WORDS = {
    "ctrl", "control", "shift", "alt", "super", "meta", "win", "windows", "enter", "return", "esc", "escape",
    "space", "spacebar", "tab", "backspace", "delete", "del", "up", "down", "left", "right", "home", "end",
    "pageup", "pagedown", "page up", "page down", "capslock", "insert",
}
FILLER = re.compile(r"\b(the|a|an|on|button|icon|link|tab|menu|option|item|thing)\b", re.I)
CORNERS = {
    "top left": (0.03, 0.03), "top right": (0.97, 0.03), "bottom left": (0.03, 0.97),
    "bottom right": (0.97, 0.97), "top": (0.5, 0.03), "bottom": (0.5, 0.97), "left": (0.03, 0.5),
    "right": (0.97, 0.5), "middle": (0.5, 0.5), "center": (0.5, 0.5), "centre": (0.5, 0.5),
}


def _key_parts(spec: str) -> list[str] | None:
    s = spec.lower().strip(" .!?")
    s = s.replace("page up", "pageup").replace("page down", "pagedown").replace("spacebar", "space")
    s = re.sub(r"\s*(\+|plus|and)\s*", "+", s)
    parts = [p for p in re.split(r"[+\s]+", s) if p and p not in ("key", "keys", "the", "button")]
    if not parts:
        return None
    for p in parts:
        if not (p in KEY_WORDS or len(p) == 1 or re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", p)):
            return None
    return parts


def _key_combo(parts: list[str]) -> list[dict[str, Any]]:
    *mods, last = parts
    acts = [{"do": "key", "key": m, "state": "down"} for m in mods]
    acts.append({"do": "key", "key": last, "state": "tap"})
    acts += [{"do": "key", "key": m, "state": "up"} for m in reversed(mods)]
    return acts


def _find(ctx: Any, target: str) -> dict[str, Any]:
    """find_on_screen, retried without filler words ('the play button' -> 'play')."""
    tries = [target]
    bare = re.sub(r"\s+", " ", FILLER.sub(" ", target)).strip()
    if bare and bare != target:
        tries.append(bare)
    err: Exception | None = None
    for t in tries:
        try:
            hit = ctx.call("find_on_screen", target=t)
            if not isinstance(hit, dict):            # dry run: a placeholder instead of a screenshot
                return {"x": 0, "y": 0, "text": t, "dry_run": True}
            return hit
        except FunctionError as exc:
            err = exc
    raise FunctionError(f"I can't see '{target}' on the screen") from err


def _point(ctx: Any, where: str) -> tuple[int, int] | None:
    w = where.lower().strip(" .!?")
    w = re.sub(r"^(the\s+)", "", w).replace("-", " ").replace(" corner", "")
    if w in CORNERS:
        bx, by, bw, bh = ctx.engine.control.desktop_box()
        fx, fy = CORNERS[w]
        return int(bx + fx * (bw - 1)), int(by + fy * (bh - 1))
    return None


def simple_actions(ctx: Any, instruction: str) -> list[dict[str, Any]] | None:
    text = instruction.strip().strip(" .!?")
    low = text.lower()

    m = re.match(r"^(left[- ]|right[- ]|middle[- ]|double[- ])?click(?:\s+on)?(?:\s+(.+))?$", low)
    if m:
        kind = (m.group(1) or "").strip(" -")
        target = (text[m.start(2):] if m.group(2) else "").strip()
        button = {"right": "BTN_RIGHT", "middle": "BTN_MIDDLE"}.get(kind, "BTN_LEFT")
        clicks = [{"do": "button", "button": button, "state": "tap"}]
        if kind == "double":
            clicks += [{"do": "wait", "seconds": 0.08}, {"do": "button", "button": button, "state": "tap"}]
        if not target or target.lower() in ("here", "it", "that", "this", "there"):
            return clicks
        pt = _point(ctx, target)
        if pt is None:
            hit = _find(ctx, target)
            pt = (hit["x"], hit["y"])
        return [{"do": "move", "x": pt[0], "y": pt[1], "absolute": True, "duration": 0.15},
                {"do": "wait", "seconds": 0.05}] + clicks

    m = re.match(r"^type\s+(?:out\s+)?(.+)$", text, re.I)
    if m:
        typed = m.group(1).strip()
        if len(typed) >= 2 and typed[0] in "\"'“" and typed[-1] in "\"'”":
            typed = typed[1:-1]
        return [{"do": "type", "text": typed}]

    m = re.match(r"^(?:press|hit|tap)\s+(?:the\s+)?(.+?)(?:\s+key)?$", low)
    if m:
        parts = _key_parts(m.group(1))
        return _key_combo(parts) if parts else None

    m = re.match(r"^scroll\s+(up|down)(?:\s+(a lot|a little|a bit|\d+))?", low)
    if m:
        amount = {"a lot": 10, "a little": 1, "a bit": 2}.get(m.group(2) or "", None)
        if amount is None:
            amount = int(m.group(2)) if m.group(2) and m.group(2).isdigit() else 3
        return [{"do": "scroll", "amount": amount if m.group(1) == "up" else -amount}]

    if re.match(r"^(release|let go of)\s+(everything|all|all keys)$", low) or low in ("let go", "release"):
        return [{"do": "release_all"}]
    m = re.match(r"^(hold(?:\s+down)?|release|let go of)\s+(?:the\s+)?(.+?)(?:\s+key)?$", low)
    if m:
        parts = _key_parts(m.group(2))
        if not parts or len(parts) != 1:
            return None
        return [{"do": "key", "key": parts[0], "state": "down" if m.group(1).startswith("hold") else "up"}]

    m = re.match(r"^(?:move|put)\s+(?:the\s+)?(?:mouse|cursor|pointer)\s+(?:to|over|on)\s+(.+)$", text, re.I)
    if m:
        where = m.group(1)
        pt = _point(ctx, where)
        if pt is None:
            hit = _find(ctx, where)
            pt = (hit["x"], hit["y"])
        return [{"do": "move", "x": pt[0], "y": pt[1], "absolute": True, "duration": 0.15}]
    return None
