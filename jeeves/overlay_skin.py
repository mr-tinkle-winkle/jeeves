"""The on-screen indicators in the window's theme: the microphone, the spinners, the name tags, the
answer bubbles, the right-click menu, timers and screen marks each look (and move) the theme's way.

  Classic    the original: dark rounded chips, an arc that turns
  Alive      round discs; three dots bounce while it works, ripples spread from the microphone
  Ledger     a thin brass ring with a dot orbiting it like a watch hand; serif tags with a brass edge
  Console    square tiles; a turning |/-\\ in amber, a blinking REC block; terminal boxes with a > prompt
  Brutalist  white squares with thick black borders and hard shadows; a square that snaps round in 90° steps
  Arcade     a ring of eight pixel blocks lighting up in turn; an RPG text box with a blinking arrow
  Blueprint  a dashed circle with a turning crosshair; outlined tags on drafting blue

The stage colors (Indicators > colors) still say what an agent is doing in every theme."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

from .gui.ui_kit import Theme, contrast_text
from .gui.ui_kit.skin import kind


def _a(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def font(theme: Theme, size: float, mono: bool = False) -> QFont:
    k = kind(theme)
    if mono or k == "type":
        return theme.mono_font(size)
    if k == "step":
        f = theme.body_font(size * 1.35)          # VT323 reads small
        return f
    f = theme.body_font(size)
    if k == "slam":
        from .gui.ui_kit.theme import set_weight
        set_weight(f, 800)
    return f


def colors(theme: Theme) -> dict[str, QColor]:
    """The chrome around the stage colors: tag/bubble fill, text, edge, link."""
    k = kind(theme)
    if k == "fade":
        return {"bg": QColor(0, 0, 0, 205), "bubble": QColor(0, 0, 0, 230), "text": QColor("#ffffff"),
                "edge": QColor(0, 0, 0, 0), "muted": QColor("#9a9a9a"), "link": QColor("#8ab4ff")}
    bg = QColor(theme.surface() if k not in ("slam", "draw") else theme.page_background())
    bg.setAlpha(240 if k != "draw" else 235)
    edge = {"glide": theme.pop(), "type": _a(theme.pop(), 0.6), "slam": theme.text(), "step": theme.accent(),
            "draw": theme.text(), "spring": _a(theme.text(), 0.08)}[k]
    return {"bg": bg, "bubble": bg, "text": theme.text(), "edge": edge, "muted": theme.muted(),
            "link": theme.pop() if k not in ("draw",) else theme.text()}


def radius(theme: Theme, h: float) -> float:
    return {"fade": 10.0, "spring": h / 2, "glide": 4.0}.get(kind(theme), 0.0)


def box(p: QPainter, r: QRectF, theme: Theme, fill: QColor | None = None, big: bool = False) -> None:
    """A tag, bubble, menu or timer box."""
    k = kind(theme)
    c = colors(theme)
    fill = fill or (c["bubble"] if big else c["bg"])
    rad = radius(theme, r.height()) if not big else {"fade": 12.0, "spring": 18.0, "glide": 4.0}.get(k, 0.0)
    path = QPainterPath()
    if rad:
        path.addRoundedRect(r, rad, rad)
    else:
        path.addRect(r)
    p.setRenderHint(QPainter.Antialiasing)
    if k == "slam":                                       # hard black shadow
        p.fillRect(r.translated(4, 4), QColor("#000000"))
    if k == "step":
        sh = theme.shadow()
        if sh:
            p.fillRect(r.translated(3, 3), sh[1])
    p.fillPath(path, fill)
    if c["edge"].alpha():
        w = {"slam": 3.0, "step": 2.0}.get(k, 1.0)
        p.setPen(QPen(c["edge"], w, Qt.DashLine if (k == "draw" and big) else Qt.SolidLine, Qt.SquareCap,
                      Qt.MiterJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
    if k == "step" and big:                               # RPG text box: inner line too
        p.setPen(QPen(_a(theme.accent(), 0.5), 1))
        p.drawRect(r.adjusted(5, 5, -5, -5))


def text_prefix(theme: Theme) -> str:
    return "> " if kind(theme) == "type" else ""


# ---------------------------------------------------------------------------- the microphone
def mic(p: QPainter, r: QRectF, theme: Theme, phase: float, held: bool, suspended: bool) -> None:
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    c = r.center()
    s = r.width() / 56
    flash = 0.5 + 0.5 * math.sin(phase * 2 * math.pi / 1.2)
    fg = QColor("#ffffff")
    if k == "fade":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, int(60 + 80 * flash)))
        p.drawEllipse(r)
        p.setBrush(QColor(20, 20, 20, 235))
        p.drawEllipse(r.adjusted(4, 4, -4, -4))
    elif k == "spring":                                   # ripples spreading out
        p.setBrush(Qt.NoBrush)
        for i in range(2):
            t = (phase * 0.8 + i * 0.5) % 1.0
            p.setPen(QPen(_a(theme.pop(), 0.6 * (1 - t)), 2))
            rr = r.width() / 2 * (0.7 + 0.32 * t)
            p.drawEllipse(c, rr, rr)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.pop())
        p.drawEllipse(r.adjusted(8, 8, -8, -8))
        fg = contrast_text(theme.pop())
        s *= 0.82
    elif k == "glide":
        p.setBrush(_a(theme.surface(), 0.95))
        p.setPen(QPen(theme.pop(), 1.5))
        p.drawEllipse(r.adjusted(3, 3, -3, -3))
        p.setPen(QPen(_a(theme.pop(), 0.35 + 0.4 * flash), 1))
        p.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
        fg = theme.pop()
        s *= 0.8
    elif k == "type":                                     # REC
        tile = r.adjusted(4, 4, -4, -4)
        p.fillRect(tile, _a(theme.surface(), 0.95))
        p.setPen(QPen(theme.pop(), 1))
        p.drawRect(tile)
        if int(phase * 2) % 2 == 0:
            p.fillRect(QRectF(tile.left() + 6, tile.top() + 6, 7, 7), theme.pop())
        p.setFont(theme.mono_font(max(6.0, 8 * s)))
        p.setPen(theme.pop())
        p.drawText(tile.adjusted(0, 0, 0, -4), Qt.AlignHCenter | Qt.AlignBottom, "REC")
        fg = theme.pop()
        s *= 0.6
        c = QPointF(c.x(), c.y() - 4 * s)
    elif k == "slam":
        tile = r.adjusted(2, 2, -6, -6)
        p.fillRect(tile.translated(4, 4), QColor("#000000"))
        p.fillRect(tile, theme.pop() if flash > 0.5 else QColor("#ffffff"))
        p.setPen(QPen(QColor("#000000"), 3))
        p.drawRect(tile)
        fg = QColor("#000000")
        c = tile.center()
        s *= 0.8
    elif k == "step":                                     # a pixel mic in a pixel frame
        p.setRenderHint(QPainter.Antialiasing, False)
        tile = r.adjusted(4, 4, -4, -4)
        p.fillRect(tile, theme.page_background())
        p.setPen(QPen(theme.pop() if int(phase * 3) % 2 == 0 else theme.accent(), 3))
        p.drawRect(tile)
        cell = tile.width() / 10
        mic_px = ["   ##   ", "   ##   ", "   ##   ", " # ## # ", "  ####  ", "   ##   ", "  ####  "]
        for y, line in enumerate(mic_px):
            for x, ch in enumerate(line):
                if ch == "#":
                    p.fillRect(QRectF(tile.x() + (x + 1) * cell, tile.y() + (y + 1.5) * cell, cell, cell),
                               theme.text())
        _held_suspended(p, r, theme, held, suspended)
        return
    else:                                                 # draw: crosshair circle
        p.setPen(QPen(_a(theme.text(), 0.5 + 0.4 * flash), 1, Qt.DashLine))
        p.setBrush(_a(theme.page_background(), 0.9))
        p.drawEllipse(r.adjusted(3, 3, -3, -3))
        p.setPen(QPen(theme.text(), 1))
        p.drawLine(QPointF(r.left(), c.y()), QPointF(r.left() + 8, c.y()))
        p.drawLine(QPointF(r.right() - 8, c.y()), QPointF(r.right(), c.y()))
        fg = theme.text()
        s *= 0.78
    pen = QPen(fg)
    pen.setWidthF(3 * s if k not in ("glide", "draw") else 2 * s)
    p.setPen(pen)
    p.setBrush(fg if k not in ("glide", "draw") else Qt.NoBrush)
    p.drawRoundedRect(QRectF(c.x() - 6 * s, c.y() - 15 * s, 12 * s, 20 * s), 6 * s, 6 * s)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(c.x() - 11 * s, c.y() - 10 * s, 22 * s, 20 * s), 200 * 16, 140 * 16)
    p.drawLine(QPointF(c.x(), c.y() + 10 * s), QPointF(c.x(), c.y() + 15 * s))
    _held_suspended(p, r, theme, held, suspended)


def _held_suspended(p: QPainter, r: QRectF, theme: Theme, held: bool, suspended: bool) -> None:
    k = kind(theme)
    if suspended:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        if k in ("fade", "spring", "glide", "draw"):
            p.drawEllipse(r.adjusted(4, 4, -4, -4))
        else:
            p.drawRect(r.adjusted(4, 4, -4, -4))
        _pause_bars(p, r.center())
    if held:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#ffcc00") if k == "fade" else theme.pop(), 3))
        if k in ("fade", "spring", "glide", "draw"):
            p.drawEllipse(r.adjusted(2, 2, -2, -2))
        else:
            p.drawRect(r.adjusted(2, 2, -2, -2))


def _pause_bars(p: QPainter, c: QPointF) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawRect(QRectF(c.x() - 7, c.y() - 8, 5, 16))
    p.drawRect(QRectF(c.x() + 2, c.y() - 8, 5, 16))


# ---------------------------------------------------------------------------- the spinners
def spinner(p: QPainter, r: QRectF, theme: Theme, base: QColor, phase: float, spinning: bool,
            outline: QColor | None, paused: bool) -> None:
    """base: the stage color (with its flashing alpha). spinning: False = a full steady shape (asking,
    unavailable, paused). outline: the responding outline color, or None."""
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    c = r.center()
    if k not in ("fade", "slam") and base.lightness() < 40:   # (responding is black: lost on a dark tile)
        a = base.alphaF()
        base = QColor(outline or theme.text())
        base.setAlphaF(a)
    if k == "fade":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawEllipse(r)
        width = r.width() * 0.14
        inner = r.adjusted(width, width, -width, -width)
        if outline is not None:
            p.setPen(QPen(outline, width + 4, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(inner, 0, 360 * 16)
        p.setPen(QPen(base, width, Qt.SolidLine, Qt.RoundCap))
        if not spinning:
            p.drawArc(inner, 0, 360 * 16)
        else:
            p.drawArc(inner, int((-phase * 360 * 1.4) % 360 * 16), 270 * 16)
    elif k == "spring":                                   # a disc with three bouncing dots
        p.setPen(QPen(outline, 3) if outline is not None else Qt.NoPen)
        p.setBrush(_a(theme.surface(), 0.95))
        p.drawEllipse(r.adjusted(2, 2, -2, -2))
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        d = r.width() * 0.13
        for i in range(3):
            x = c.x() + (i - 1) * d * 2.2
            if spinning:
                t = (phase * 1.6 - i * 0.18) % 1.0
                bounce = abs(math.sin(t * math.pi)) ** 0.6
                y = c.y() + d * 0.6 - bounce * r.height() * 0.2
            else:
                y = c.y()
            p.drawEllipse(QPointF(x, y), d * 0.75, d * 0.75)
    elif k == "glide":                                    # a brass ring and an orbiting dot
        p.setPen(QPen(outline if outline is not None else _a(theme.pop(), 0.55), 1.5))
        p.setBrush(_a(theme.surface(), 0.92))
        rr = r.adjusted(4, 4, -4, -4)
        p.drawEllipse(rr)
        p.setPen(QPen(base, 2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rr.adjusted(6, 6, -6, -6))
        ang = (phase * 0.9 * 2 * math.pi) if spinning else -math.pi / 2
        rad = rr.width() / 2 - 6
        dot = QPointF(c.x() + rad * math.cos(ang), c.y() + rad * math.sin(ang))
        p.setPen(Qt.NoPen)
        p.setBrush(base)
        p.drawEllipse(dot, 4.5, 4.5)
        p.setPen(QPen(base, 1.2))                         # the watch hand
        p.drawLine(c, dot)
    elif k == "type":                                     # | / - \ in a terminal tile
        tile = r.adjusted(4, 4, -4, -4)
        p.fillRect(tile, _a(theme.surface(), 0.95))
        p.setPen(QPen(outline if outline is not None else _a(base, 0.7), 1 if outline is None else 2))
        p.drawRect(tile)
        p.setFont(theme.mono_font(r.height() * 0.42))
        p.setPen(base)
        ch = "|/-\\"[int(phase * 8) % 4] if spinning else "#"
        p.drawText(tile, Qt.AlignCenter, ch)
    elif k == "slam":                                     # a square snapping round in quarter turns
        tile = r.adjusted(2, 2, -6, -6)
        p.fillRect(tile.translated(4, 4), QColor("#000000"))
        p.fillRect(tile, QColor("#ffffff"))
        p.setPen(QPen(outline if outline is not None else QColor("#000000"), 3))
        p.drawRect(tile)
        p.save()
        p.translate(tile.center())
        if spinning:
            p.rotate(90 * (int(phase * 2.5) % 4))
        s = tile.width() * 0.22
        p.fillRect(QRectF(-s, -s, 2 * s, 2 * s), base)
        p.fillRect(QRectF(s * 0.2, -s, s * 0.8, s * 0.8), QColor("#000000"))
        p.restore()
    elif k == "step":                                     # eight pixel blocks lighting up in turn
        p.setRenderHint(QPainter.Antialiasing, False)
        cell = r.width() * 0.15
        lit = int(phase * 10) % 8
        tile = r.adjusted(2, 2, -2, -2)
        sh = theme.shadow()
        if sh:
            p.fillRect(tile.translated(3, 3), sh[1])
        p.fillRect(tile, theme.page_background())
        p.setPen(QPen(outline if outline is not None else theme.accent(), 2))
        p.drawRect(tile)
        for i in range(8):
            ang = i * math.pi / 4 - math.pi / 2
            rad = r.width() / 2 - cell - 3
            x = c.x() + rad * math.cos(ang) - cell / 2
            y = c.y() + rad * math.sin(ang) - cell / 2
            on = (not spinning) or (i - lit) % 8 in (0, 7, 6)
            col = QColor(base) if on else _a(base, 0.22)
            if spinning and (i - lit) % 8 in (7, 6):
                col = _a(base, 0.6 if (i - lit) % 8 == 7 else 0.4)
            p.fillRect(QRectF(round(x), round(y), round(cell), round(cell)), col)
    else:                                                 # draw: dashed circle and a turning crosshair
        line = theme.text()
        p.setPen(QPen(outline if outline is not None else _a(line, 0.55), 1, Qt.DashLine))
        p.setBrush(_a(theme.page_background(), 0.85))
        rr = r.adjusted(4, 4, -4, -4)
        p.drawEllipse(rr)
        p.setPen(QPen(base, 2))
        p.setBrush(Qt.NoBrush)
        span = 360 if not spinning else 110
        start = 0 if not spinning else int(-phase * 220) % 360
        p.drawArc(rr.adjusted(6, 6, -6, -6), start * 16, span * 16)
        p.save()
        p.translate(c)
        if spinning:
            p.rotate(phase * 90)
        p.setPen(QPen(line, 1))
        h = rr.width() / 2 + 2
        p.drawLine(QPointF(-h, 0), QPointF(-4, 0))
        p.drawLine(QPointF(4, 0), QPointF(h, 0))
        p.drawLine(QPointF(0, -h), QPointF(0, -4))
        p.drawLine(QPointF(0, 4), QPointF(0, h))
        p.restore()
    if paused:
        _pause_bars(p, c)


# ---------------------------------------------------------------------------- timers and screen marks
def timer_bar(p: QPainter, bar: QRectF, theme: Theme, frac: float) -> None:
    k = kind(theme)
    if k == "fade":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#2f6fff"))
        p.drawRoundedRect(bar, 2, 2)
        return
    if k == "type":                                       # [#####.....]
        n = max(4, int(bar.width() // 8))
        lit = int(n * frac + 1e-6)
        cw = bar.width() / n
        for i in range(n):
            p.fillRect(QRectF(bar.left() + i * cw + 1, bar.top(), cw - 2, bar.height()),
                       theme.pop() if i < lit else _a(theme.muted(), 0.25))
        return
    if k == "step":
        n = max(4, int(bar.width() // 10))
        lit = int(n * frac + 1e-6)
        cw = bar.width() / n
        for i in range(lit):
            p.fillRect(QRectF(bar.left() + i * cw + 1, bar.top(), cw - 2, bar.height()), theme.pop())
        return
    p.fillRect(bar, {"slam": theme.pop(), "draw": theme.text()}.get(k, theme.pop()))


def mark(p: QPainter, c: QPointF, r: float, theme: Theme, age: float) -> None:
    """Where an agent points on screen."""
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    if k == "fade":
        p.setPen(QPen(QColor(0, 0, 0, 160), 7))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(QColor("#ff3b30"), 4))
        p.drawEllipse(c, r, r)
        return
    col = theme.pop() if k != "draw" else QColor("#ffffff")
    if k in ("slam", "step", "type"):                     # square brackets round the spot
        for pen in (QPen(QColor(0, 0, 0, 170), 7, Qt.SolidLine, Qt.SquareCap), QPen(col, 4, Qt.SolidLine, Qt.SquareCap)):
            p.setPen(pen)
            arm = r * 0.45
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                corner = QPointF(c.x() + sx * r, c.y() + sy * r)
                p.drawLine(corner, QPointF(corner.x() - sx * arm, corner.y()))
                p.drawLine(corner, QPointF(corner.x(), corner.y() - sy * arm))
        return
    for pen in (QPen(QColor(0, 0, 0, 150), 6), QPen(col, 3, Qt.DashLine if k == "draw" else Qt.SolidLine)):
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r, r)
    if k in ("draw", "glide"):                            # crosshair
        p.setPen(QPen(col, 1.5))
        p.drawLine(QPointF(c.x() - r - 10, c.y()), QPointF(c.x() - r * 0.4, c.y()))
        p.drawLine(QPointF(c.x() + r * 0.4, c.y()), QPointF(c.x() + r + 10, c.y()))
        p.drawLine(QPointF(c.x(), c.y() - r - 10), QPointF(c.x(), c.y() - r * 0.4))
        p.drawLine(QPointF(c.x(), c.y() + r * 0.4), QPointF(c.x(), c.y() + r + 10))
    if k == "spring":                                     # and a ripple
        t = (age * 1.2) % 1.0
        p.setPen(QPen(_a(col, 0.6 * (1 - t)), 2))
        p.drawEllipse(c, r * (1 + 0.5 * t), r * (1 + 0.5 * t))
