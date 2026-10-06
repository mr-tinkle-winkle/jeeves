"""Screen Reading: find something on screen, read text on screen.

Screenshots come from ``grim`` (wlroots/Hyprland), ``spectacle`` (KDE) or
``gnome-screenshot``/``import`` as fallbacks. Text is found with Tesseract
(TSV output gives every word's box). Finding non-text objects ("the red
button", "the play icon") uses the local response model when it is a vision
model; otherwise Jeeves looks for the object's name as text.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Any
from pathlib import Path

from ...daemon import desktop as dk
from ...util import desktop, graphical_env, normalize, run, similarity, which
from ..base import Arg, FunctionError, partial

REGIONS = ["anywhere", "top", "bottom", "left", "right", "middle", "top-left", "top-right", "bottom-left",
           "bottom-right"]


last_tool = ""          # which tool took the last screenshot (for jeeves doctor)


def _portal_cmd(path: Path) -> list[str]:
    exe = os.environ.get("JEEVES_BIN") or which("jeeves")
    if exe:
        return [exe, "screenshot-portal", str(path)]
    return [sys.executable, "-m", "jeeves.screenshot_portal", str(path)]


def screenshot_commands(path: Path) -> list[tuple[str, list[str]]]:
    """Screenshot tools to try, best first for this desktop. KWin only lets Spectacle
    (and the desktop portal) capture silently; wlroots compositors and Hyprland use grim."""
    p = str(path)
    tools = {
        "spectacle": ["spectacle", "-b", "-n", "-f", "-o", p] if which("spectacle") else None,
        "portal": _portal_cmd(path),
        "grim": ["grim", p] if which("grim") else None,
        "gnome-screenshot": ["gnome-screenshot", "-f", p] if which("gnome-screenshot") else None,
        "import": ["import", "-window", "root", p] if which("import") else None,
    }
    order = (["spectacle", "portal", "grim"] if desktop() == "kde" else
             ["grim", "portal", "spectacle", "gnome-screenshot", "import"])
    return [(name, tools[name]) for name in order if tools.get(name)]


def screenshot() -> Path:
    fd, name = tempfile.mkstemp(prefix="jeeves-shot-", suffix=".png")
    os.close(fd)
    path = Path(name)
    errors = []
    env = graphical_env()
    for tool, cmd in screenshot_commands(path):
        path.write_bytes(b"")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{tool}: {exc}")
            continue
        if out.returncode == 0 and path.exists() and path.stat().st_size > 0:
            global last_tool
            last_tool = tool
            return path
        last = (out.stderr or out.stdout or "").strip().splitlines()
        errors.append(f"{tool}: {last[-1] if last else f'exit {out.returncode}'}")
    path.unlink(missing_ok=True)
    if not errors:
        raise FunctionError("no screenshot tool installed (spectacle, grim or the desktop portal)")
    raise FunctionError("couldn't take a screenshot -- " + "; ".join(errors))


class Mapper:
    """Screenshot pixels <-> desktop (logical) coordinates, which mouse moves use.
    A HiDPI or multi-monitor screenshot is in physical pixels and starts at 0,0;
    the desktop may be scaled and may start elsewhere."""

    def __init__(self, image_size: tuple[int, int], box: tuple[int, int, int, int] | None = None) -> None:
        self.iw, self.ih = max(1, image_size[0]), max(1, image_size[1])
        if box is None:
            try:
                outs = dk.outputs()
                known = not (len(outs) == 1 and outs[0].name == "default")
                box = dk.desktop_box() if known else None
            except Exception:
                box = None
        self.box = box or (0, 0, self.iw, self.ih)     # layout unknown: assume 1 px = 1 unit
        bx, by, bw, bh = self.box
        self.sx, self.sy = bw / self.iw, bh / self.ih

    def point(self, x: float, y: float) -> tuple[int, int]:
        return int(round(self.box[0] + x * self.sx)), int(round(self.box[1] + y * self.sy))

    def rect_to_image(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, w, h = rect
        return (int((x - self.box[0]) / self.sx), int((y - self.box[1]) / self.sy),
                int(w / self.sx), int(h / self.sy))


def ocr_words(image: Path) -> list[dict]:
    """[{text, x, y, w, h, conf, line}] from tesseract TSV."""
    if not which("tesseract"):
        raise FunctionError("tesseract isn't installed (needed to read the screen)")
    out = run(["tesseract", str(image), "-", "tsv"], timeout=60)
    if out.returncode != 0:
        raise FunctionError(f"tesseract failed: {out.stderr.strip()[:200]}")
    words = []
    lines = out.stdout.splitlines()
    for row in lines[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < 30:
            continue
        words.append({"text": cols[11], "x": int(cols[6]), "y": int(cols[7]), "w": int(cols[8]),
                      "h": int(cols[9]), "conf": conf, "line": (cols[2], cols[3], cols[4])})
    return words


def region_rect(region, width: int, height: int) -> tuple[int, int, int, int]:
    if isinstance(region, dict):
        return int(region.get("x", 0)), int(region.get("y", 0)), int(region.get("w", width)), int(region.get("h", height))
    r = (region or "anywhere").lower().replace(" ", "-").replace("the-", "").replace("center", "middle")
    w3, h3 = width // 3, height // 3
    table = {
        "anywhere": (0, 0, width, height), "top": (0, 0, width, h3), "bottom": (0, 2 * h3, width, h3),
        "left": (0, 0, w3, height), "right": (2 * w3, 0, w3, height), "middle": (w3, h3, w3, h3),
        "top-left": (0, 0, w3, h3), "top-right": (2 * w3, 0, w3, h3), "bottom-left": (0, 2 * h3, w3, h3),
        "bottom-right": (2 * w3, 2 * h3, w3, h3),
    }
    return table.get(r, table["anywhere"])


SCREENS = "all, current (the one you're working on), primary, left, right, second, other, or a monitor name"
ORDINALS = {"first": 0, "1": 0, "one": 0, "second": 1, "2": 1, "two": 1, "third": 2, "3": 2, "three": 2}


def _current_output(outs: list) -> Any:
    """The monitor you're working on: the focused window's, else the mouse's, else the primary."""
    points = []
    try:
        f = dk.focused()
        if f and f.w and f.h:
            points.append((f.x + f.w // 2, f.y + f.h // 2))
    except Exception:  # noqa: BLE001 -- no window info: try the mouse
        pass
    try:
        points.append(dk.mouse_position())
    except Exception:  # noqa: BLE001
        pass
    for x, y in points:
        hit = next((o for o in outs if o.x <= x < o.x + o.w and o.y <= y < o.y + o.h), None)
        if hit is not None:
            return hit
    return next((o for o in outs if o.primary), outs[0])


def pick_outputs(screen: str | None, outs: list | None = None) -> list:
    """Which monitors a request means. [] = all of them (or the layout is unknown)."""
    try:
        outs = outs if outs is not None else dk.outputs()
    except Exception:  # noqa: BLE001
        return []
    if len(outs) < 2 or (len(outs) == 1 and outs[0].name == "default"):
        return []
    s = str(screen or "all").strip().lower().replace(" screen", "").replace(" monitor", "")
    s = s.removeprefix("the ").removeprefix("my ")
    if s in ("", "all", "every", "both", "anywhere", "everywhere", "all screens", "any"):
        return []
    by_x = sorted(outs, key=lambda o: (o.x, o.y))
    if s in ("current", "this", "focused", "active", "here", "mine"):
        return [_current_output(outs)]
    if s in ("primary", "main"):
        return [next((o for o in outs if o.primary), by_x[0])]
    if s == "left":
        return [by_x[0]]
    if s == "right":
        return [by_x[-1]]
    if s in ("middle", "center", "centre"):
        return [by_x[len(by_x) // 2]]
    if s in ("top", "upper"):
        return [min(outs, key=lambda o: o.y)]
    if s in ("bottom", "lower"):
        return [max(outs, key=lambda o: o.y)]
    if s in ORDINALS:
        return [by_x[min(ORDINALS[s], len(by_x) - 1)]]
    if s in ("other", "second screen", "secondary"):
        cur = _current_output(outs)
        return [o for o in outs if o is not cur][:1]
    named = [o for o in outs if o.name.lower() == s]
    return named or []


def describe_output(o: Any, outs: list) -> str:
    """'your left screen' etc. for speaking."""
    if len(outs) < 2:
        return "your screen"
    by_x = sorted(outs, key=lambda m: (m.x, m.y))
    i = by_x.index(o)
    if len(outs) == 2:
        return "your left screen" if i == 0 else "your right screen"
    return {0: "your left screen", len(by_x) - 1: "your right screen"}.get(i, "your middle screen")


def _rect_for(region, width: int, height: int, mp: Mapper, screen: str | None = None) -> tuple[int, int, int, int]:
    """Where to look, in screenshot pixels. A named region (top, bottom-left...) is relative to the
    chosen monitor (or the whole desktop for 'all'); an {x,y,w,h} region is in desktop coordinates
    (the same ones find_on_screen returns)."""
    if isinstance(region, dict):
        return mp.rect_to_image(region_rect(region, width, height))
    picked = pick_outputs(screen)
    if picked:
        o = picked[0]
        ox, oy, ow, oh = mp.rect_to_image((o.x, o.y, o.w, o.h))
        ox, oy = max(0, ox), max(0, oy)
        ow, oh = min(ow, width - ox), min(oh, height - oy)
        rx, ry, rw, rh = region_rect(region, ow, oh)
        return ox + rx, oy + ry, rw, rh
    return region_rect(region, width, height)


def _inside(word: dict, rect: tuple[int, int, int, int]) -> bool:
    x, y, w, h = rect
    cx, cy = word["x"] + word["w"] / 2, word["y"] + word["h"] / 2
    return x <= cx <= x + w and y <= cy <= y + h


def _expand(rect, width, height, factor=1.5):
    x, y, w, h = rect
    nw, nh = min(width, int(w * factor) + 1), min(height, int(h * factor) + 1)
    nx, ny = max(0, x - (nw - w) // 2), max(0, y - (nh - h) // 2)
    return nx, ny, min(nw, width - nx), min(nh, height - ny)


def _per_monitor(words: list[dict], mp: Mapper, region, width: int, height: int) -> str | None:
    try:
        outs = dk.outputs()
    except Exception:  # noqa: BLE001
        return None
    if len(outs) < 2 or mp.box == (0, 0, mp.iw, mp.ih) and outs[0].name == "default":
        return None
    parts = []
    for o in sorted(outs, key=lambda m: (m.x, m.y)):
        rect = _rect_for(region, width, height, mp, o.name)
        lines = _group_lines([w for w in words if _inside(w, rect)])
        if lines:
            parts.append(f"[{describe_output(o, outs).removeprefix('your ')}]\n" + "\n".join(l["text"] for l in lines))
    return "\n\n".join(parts)


def _expand_within(rect, bound, factor=1.5):
    """Grow rect around its centre, staying inside bound (a monitor)."""
    bx, by, bw, bh = bound
    x, y, w, h = rect
    nw, nh = min(bw, int(w * factor) + 1), min(bh, int(h * factor) + 1)
    nx = min(max(bx, x - (nw - w) // 2), bx + bw - nw)
    ny = min(max(by, y - (nh - h) // 2), by + bh - nh)
    return nx, ny, nw, nh


def _image_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return dk.screen_size()


def _group_lines(words: list[dict]) -> list[dict]:
    lines: dict = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w)
    out = []
    for key, ws in lines.items():
        x0, y0 = min(w["x"] for w in ws), min(w["y"] for w in ws)
        x1, y1 = max(w["x"] + w["w"] for w in ws), max(w["y"] + w["h"] for w in ws)
        out.append({"text": " ".join(w["text"] for w in ws), "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "line": key})
    return sorted(out, key=lambda l: (l["y"], l["x"]))


@partial(
    "find_on_screen",
    "Looks for something on the screen and returns where it is, in absolute pixels.",
    args=[Arg("target", "string", "What to look for: text shown on screen, or a short description of an object"),
          Arg("region", "region", "Where to look", required=False, default="anywhere", choices=None),
          Arg("screen", "string", f"Which monitor: {SCREENS}", required=False, default="all"),
          Arg("bounds", "string", "Also return the object's outline: 'rectangle' (4 points) or 'polygon' "
              "(more points, more precise)", required=False, default="none",
              choices=["none", "rectangle", "polygon"])],
    how="Takes a screenshot, reads every word with OCR and picks the best match for the target (multi-word "
        "targets match whole lines). With a vision-capable local response model, objects without text are "
        "found by asking the model.",
    returns="{x, y} centre position (plus 'bounds': list of points when requested), or an error if not found",
    category="screen",
    dry_run_safe=False,
)
def find_on_screen(ctx, target, region="anywhere", bounds="none", screen="all"):
    shot = screenshot()
    try:
        width, height = _image_size(shot)
        mp = Mapper((width, height))
        words = ocr_words(shot)
        rect = _rect_for(region, width, height, mp, screen)
        candidates = [w for w in words if _inside(w, rect)] + _group_lines([w for w in words if _inside(w, rect)])
        best, score = None, 0.0
        t = normalize(str(target))
        for c in candidates:
            s = similarity(t, c["text"])
            if t and t in normalize(c["text"]):
                s = max(s, 0.85 + 0.15 * len(t) / max(1, len(normalize(c["text"]))))
            if s > score:
                best, score = c, s
        if best is None or score < 0.6:
            vision = ctx.engine.models.vision_locate(shot, str(target)) if ctx.engine.models else None
            if vision:
                return vision
            raise FunctionError(f"couldn't find '{target}' on screen")
        cx, cy = mp.point(best["x"] + best["w"] / 2, best["y"] + best["h"] / 2)
        result = {"x": cx, "y": cy, "text": best["text"], "score": round(score, 2)}
        if bounds in ("rectangle", "polygon"):
            x, y, w, h = best["x"], best["y"], best["w"], best["h"]
            pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            if bounds == "polygon":
                # per-word boxes along the line trace the outline more tightly
                members = [wd for wd in words if wd["line"] == best.get("line")] if "line" in best else []
                if len(members) > 1:
                    top = [(m["x"], m["y"]) for m in members] + [(members[-1]["x"] + members[-1]["w"], members[-1]["y"])]
                    bottom = [(m["x"] + m["w"], m["y"] + m["h"]) for m in reversed(members)] + \
                             [(members[0]["x"], members[0]["y"] + members[0]["h"])]
                    pts = top + bottom
            result["bounds"] = [dict(zip("xy", mp.point(px, py))) for px, py in pts]
        return result
    finally:
        shot.unlink(missing_ok=True)


@partial(
    "read_screen_text",
    "Reads the text shown on screen, optionally only in part of the screen.",
    args=[Arg("region", "region", "Where to read: anywhere, top, middle, bottom-left, ... or {x,y,w,h}",
              required=False, default="anywhere"),
          Arg("screen", "string", f"Which monitor: {SCREENS}", required=False, default="current")],
    how="OCR on a screenshot. With a vague region (e.g. 'middle'), if no text is there the area grows until "
        "text is found.",
    returns="the text, line by line",
    category="screen",
)
def read_screen_text(ctx, region="anywhere", screen="current"):
    shot = screenshot()
    try:
        width, height = _image_size(shot)
        words = ocr_words(shot)
        mp = Mapper((width, height))
        if not isinstance(region, dict) and not pick_outputs(screen):
            per = _per_monitor(words, mp, region, width, height)
            if per is not None:                    # several monitors: one section each, left to right
                return per
        rect = _rect_for(region, width, height, mp, screen)
        bound = _rect_for("anywhere", width, height, mp, screen)       # the chosen monitor (or everything)
        for _ in range(8):
            inside = [w for w in words if _inside(w, rect)]
            if inside or rect == bound:
                return "\n".join(l["text"] for l in _group_lines(inside))
            if isinstance(region, dict):
                break
            rect = _expand_within(rect, bound)
        return ""
    finally:
        shot.unlink(missing_ok=True)
