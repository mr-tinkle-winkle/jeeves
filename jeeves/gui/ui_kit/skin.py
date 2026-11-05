"""Per-theme drawing of the kit's controls -- section boxes, checkboxes, radio buttons, text fields,
buttons, sliders, combo boxes and scroll bars -- so a page's contents look like its theme, not just
its frame. Keyed by the theme's motion (one per look):

  fade    Classic    soft filled cards, toggle switches, quiet filled fields
  spring  Alive      round cards with a colored dot, springy pill switches, pill fields and buttons
  glide   Ledger     no boxes: serif titles over a double brass rule, written-on-a-line fields, ring ticks
  type    Console    boxes drawn with a title in the border, [x] checks, [ bracketed ] buttons
  slam    Brutalist  thick black borders, yellow title blocks, hard shadows, X checks
  step    Arcade     double pixel borders, pixel checks, bevelled fields, buttons on a shadow
  draw    Blueprint  numbered sections with crop marks, drafting crosses, dashed lines to write on

Anything else (a custom theme with another motion) gets Classic's way."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from .rounded_rect import rounded_rect_path
from .theme import Theme, contrast_text

KNOWN = ("fade", "spring", "glide", "type", "slam", "step", "draw")


def kind(theme: Theme) -> str:
    m = theme.motion
    return m if m in KNOWN else "fade"


def _alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def _rr(rect: QRectF, r: float) -> QPainterPath:
    if r <= 0:
        p = QPainterPath()
        p.addRect(rect)
        return p
    return rounded_rect_path(rect, min(r, rect.width() / 2, rect.height() / 2))


def line_color(theme: Theme) -> QColor:
    """Rules and outlines that must show on the page (the accent is too dark in some looks)."""
    k = kind(theme)
    if k in ("type",):
        return _alpha(theme.muted(), 0.45)
    if k == "glide":
        return theme.pop()
    if k == "draw":
        return _alpha(theme.text(), 0.8)
    if k == "slam":
        return theme.text()
    return theme.accent()


# ---------------------------------------------------------------------------- section boxes
def group_margins(theme: Theme, title_h: int) -> tuple[int, int, int, int]:
    k = kind(theme)
    bw = int(theme.border_width)
    sh = (theme.shadow() or (0, None))[0]
    if k == "glide":
        return 2, title_h + 14, 2, 12
    if k == "fade":
        return 16, title_h + 12, 16, 14
    if k == "spring":
        return 18, title_h + 14, 18, 16
    if k == "slam":
        return 16 + bw, title_h + 10 + bw, 16 + bw + 6, 14 + bw + 6
    if k == "step":
        return 14 + bw, title_h + 10 + bw, 14 + bw + sh, 12 + bw + sh
    if k == "draw":
        return 14, title_h + 14, 14, 14
    return 12 + bw, title_h + 6 + bw, 12 + bw + sh, 10 + bw + sh


def paint_group(p: QPainter, widget, title: str, theme: Theme, title_h: int, number: int = 0) -> None:
    k = kind(theme)
    w, h = widget.width(), widget.height()
    bg = theme.page_background()
    text = theme.text()
    p.setRenderHint(QPainter.Antialiasing)
    if k == "glide":                                      # Ledger: serif title over a double brass rule
        f = theme.display_font(15, 600)
        f.setItalic(True)
        p.setFont(f)
        p.setPen(text)
        fm = QFontMetricsF(f)
        p.drawText(QPointF(2, fm.ascent() + 2), title)
        y = fm.height() + 6
        p.fillRect(QRectF(0, y, w, 1.2), theme.pop())
        p.fillRect(QRectF(0, y + 3, w, 0.6), _alpha(theme.pop(), 0.5))
        return
    if k == "fade":                                       # Classic: a soft filled card, the title inside it
        card = QRectF(0.5, 0.5, w - 1, h - 1)
        p.fillPath(_rr(card, 14), _mix(theme.page_background(), theme.surface(), 0.85))
        p.setPen(QPen(_alpha(theme.text(), 0.06), 1))
        p.drawPath(_rr(card, 14))
        f = theme.body_font(max(7.0, theme.body_font().pointSizeF() - 0.5), 650)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        p.setFont(f)
        p.setPen(theme.muted())
        p.drawText(QRectF(16, 6, w - 32, title_h + 4), Qt.AlignVCenter | Qt.AlignLeft, title.upper())
        return
    if k == "spring":                                     # Alive: a round card, a colored dot, a glow at the top
        card = QRectF(0, 0, w, h)
        p.fillPath(_rr(card, 22), theme.surface())
        p.save()
        p.setClipPath(_rr(card, 22))
        from PySide6.QtGui import QLinearGradient
        g = QLinearGradient(0, 0, 0, min(90.0, h))
        g.setColorAt(0, _alpha(theme.pop(), 0.10))
        g.setColorAt(1, _alpha(theme.pop(), 0.0))
        p.fillRect(QRectF(0, 0, w, min(90.0, h)), g)
        p.restore()
        p.setBrush(theme.pop())
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(22, 8 + title_h / 2), 4.5, 4.5)
        p.setFont(theme.display_font(12, 700))
        p.setPen(text)
        p.drawText(QRectF(34, 8, w - 40, title_h), Qt.AlignVCenter | Qt.AlignLeft, title)
        return
    if k == "type":                                       # Console: a box with the title cut into its border
        f = theme.mono_font()
        p.setFont(f)
        fm = QFontMetricsF(f)
        box = QRectF(0.5, title_h / 2, w - 1, h - title_h / 2 - 0.5)
        p.setPen(QPen(line_color(theme), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(box)
        if not title:
            return
        label = f"[ {title.lower()} ]"
        gap = QRectF(14, 0, fm.horizontalAdvance(label) + 8, title_h)
        p.fillRect(gap, bg)
        p.setPen(theme.pop())
        p.drawText(gap, Qt.AlignCenter, label)
        return
    if k == "slam":                                       # Brutalist: thick border, hard shadow, a yellow title block
        bw = theme.border_width
        box = QRectF(bw / 2, title_h / 2, w - bw - 6, h - title_h / 2 - bw / 2 - 6)
        p.fillRect(box.translated(6, 6), theme.text())
        p.fillRect(box, theme.surface())
        p.setPen(QPen(theme.text(), bw, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawRect(box)
        if not title:
            return
        f = theme.display_font(12, 900)
        p.setFont(f)
        fm = QFontMetricsF(f)
        tag = QRectF(14, 0, fm.horizontalAdvance(title) + 20, title_h)
        p.fillRect(tag, theme.pop())
        p.setPen(QPen(theme.text(), 2.5))
        p.drawRect(tag)
        p.setPen(QColor("#000000"))
        p.drawText(tag, Qt.AlignCenter, title)
        return
    if k == "step":                                       # Arcade: double pixel border, shadow, title chip
        bw = theme.border_width
        sh = (theme.shadow() or (0, None))
        box = QRectF(bw / 2, title_h / 2, w - bw - (sh[0] or 0), h - title_h / 2 - bw / 2 - (sh[0] or 0))
        if sh[1] is not None:
            p.fillRect(box.translated(sh[0], sh[0]), sh[1])
        p.fillRect(box, theme.page_background())
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(theme.accent(), bw, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawRect(box)
        p.setPen(QPen(_alpha(theme.accent(), 0.55), 1))
        p.drawRect(box.adjusted(bw + 3, bw + 3, -bw - 3, -bw - 3))
        if not title:
            return
        f = theme.display_font(9, 400)
        p.setFont(f)
        fm = QFontMetricsF(f)
        tag = QRectF(16, 0, fm.horizontalAdvance(title) + 18, title_h)
        p.fillRect(tag, theme.accent())
        p.setPen(theme.page_background())
        p.drawText(tag, Qt.AlignCenter, title)
        return
    if k == "draw":                                       # Blueprint: numbered title, thin frame, crop marks
        line = line_color(theme)
        f = theme.display_font(12, 600)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        p.setFont(f)
        fm = QFontMetricsF(f)
        num = f"{number:02d}" if number else "--"
        p.setPen(theme.muted())
        p.drawText(QPointF(8, fm.ascent() + 2), num)
        nx = 8 + fm.horizontalAdvance(num) + 10
        p.setPen(theme.text())
        p.drawText(QPointF(nx, fm.ascent() + 2), title)
        tx = nx + fm.horizontalAdvance(title) + 10
        y = fm.height() / 2 + 2
        p.setPen(QPen(_alpha(line, 0.5), 1, Qt.DashLine))
        p.drawLine(QPointF(tx, y), QPointF(w - 8, y))
        box = QRectF(6.5, title_h + 2.5, w - 13, h - title_h - 9)
        p.setPen(QPen(_alpha(line, 0.55), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(box)
        p.setPen(QPen(line, 1.2))
        for cx, cy, dx, dy in ((box.left(), box.top(), -1, -1), (box.right(), box.top(), 1, -1),
                               (box.left(), box.bottom(), -1, 1), (box.right(), box.bottom(), 1, 1)):
            p.drawLine(QPointF(cx, cy), QPointF(cx + 6 * dx, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + 6 * dy))
        return


# ---------------------------------------------------------------------------- checkboxes and radios
def check_size(theme: Theme, fm: QFontMetricsF | None = None) -> tuple[float, float]:
    k = kind(theme)
    if k == "fade":
        return 34.0, 18.0
    if k == "spring":
        return 40.0, 22.0
    if k == "type":
        f = fm or QFontMetricsF(theme.mono_font())
        return f.horizontalAdvance("[x]"), f.height()
    if k == "slam":
        return 22.0, 22.0
    if k == "glide":
        return 16.0, 16.0
    return 18.0, 18.0


def paint_check(p: QPainter, r: QRectF, theme: Theme, on: float, hover: bool, enabled: bool = True) -> None:
    """on: 0..1 (animated between off and on)."""
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    if not enabled:
        p.setOpacity(0.45)
    if k in ("fade", "spring"):                            # a switch whose knob slides (and springs, in Alive)
        track_off = theme.highlight() if k == "fade" else theme.accent()
        track = _mix(track_off, theme.pop(), max(0.0, min(1.0, on)))
        if hover:
            track = track.lighter(112)
        p.fillPath(_rr(r, r.height() / 2), track)
        pad = 2.5 if k == "fade" else 3
        d = r.height() - 2 * pad
        x = r.x() + pad + (r.width() - 2 * pad - d) * on
        knob = QRectF(x, r.y() + pad, d, d)
        if k == "spring":                                 # stretches as it moves
            stretch = 4 * (1 - abs(on * 2 - 1)) if 0 < on < 1 else 0
            knob = knob.adjusted(-stretch / 2, 0, stretch / 2, 0)
        p.fillPath(_rr(knob, d / 2), QColor("#ffffff") if on > 0.5 or k == "spring" else theme.muted())
        return
    if k == "glide":                                      # a brass ring, a dot when ticked
        p.setPen(QPen(theme.pop() if on > 0.01 or hover else theme.muted(), 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(r.adjusted(0.7, 0.7, -0.7, -0.7))
        if on > 0.01:
            rad = (r.width() / 2 - 4) * on
            p.setBrush(theme.pop())
            p.setPen(Qt.NoPen)
            p.drawEllipse(r.center(), rad, rad)
        return
    if k == "type":                                       # [x]
        f = theme.mono_font()
        p.setFont(f)
        p.setPen(theme.text() if not hover else theme.pop())
        p.drawText(r, Qt.AlignCenter, "[ ]")
        if on > 0.5:
            p.setPen(theme.pop())
            p.drawText(r, Qt.AlignCenter, " x ")
        return
    if k == "slam":                                       # a thick square, yellow with a black X when ticked
        bw = 3.0
        box = r.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2)
        p.fillRect(box, theme.pop() if on > 0.5 else (theme.highlight() if hover else theme.surface()))
        p.setPen(QPen(theme.text(), bw, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawRect(box)
        if on > 0.5:
            p.setPen(QPen(QColor("#000000"), 3, Qt.SolidLine, Qt.SquareCap))
            m = 5
            p.drawLine(QPointF(box.left() + m, box.top() + m), QPointF(box.right() - m, box.bottom() - m))
            p.drawLine(QPointF(box.right() - m, box.top() + m), QPointF(box.left() + m, box.bottom() - m))
        return
    if k == "step":                                       # a pixel box and a pixel tick
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(r, theme.page_background())
        p.setPen(QPen(theme.pop() if hover else theme.accent(), 3, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawRect(r.adjusted(1.5, 1.5, -1.5, -1.5))
        if on > 0.5:
            c = r.width() / 9
            for x, y in ((2, 4), (3, 5), (4, 6), (5, 5), (6, 4), (7, 3), (6, 2)):
                if (x, y) == (6, 2):
                    continue
                p.fillRect(QRectF(r.x() + x * c, r.y() + y * c, c + 0.5, c + 0.5), theme.pop())
        return
    # draw (Blueprint): an outlined square and a drafting cross that draws itself
    line = line_color(theme)
    p.setPen(QPen(line if not hover else theme.text(), 1.2))
    p.setBrush(Qt.NoBrush)
    p.drawRect(r.adjusted(0.6, 0.6, -0.6, -0.6))
    if on > 0.01:
        m = 4
        a, b = QPointF(r.left() + m, r.top() + m), QPointF(r.right() - m, r.bottom() - m)
        c, d = QPointF(r.right() - m, r.top() + m), QPointF(r.left() + m, r.bottom() - m)
        p.setPen(QPen(theme.text(), 1.6))
        t1 = min(1.0, on * 2)
        t2 = max(0.0, on * 2 - 1)
        p.drawLine(a, a + (b - a) * t1)
        if t2 > 0:
            p.drawLine(c, c + (d - c) * t2)


def paint_radio(p: QPainter, r: QRectF, theme: Theme, on: bool, hover: bool) -> None:
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    if k == "type":
        p.setFont(theme.mono_font())
        p.setPen(theme.pop() if on else (theme.pop() if hover else theme.text()))
        p.drawText(r, Qt.AlignCenter, "(*)" if on else "( )")
        return
    if k == "step":
        p.setRenderHint(QPainter.Antialiasing, False)
        p.setPen(QPen(theme.pop() if hover else theme.accent(), 3))
        p.setBrush(theme.page_background())
        p.drawRect(r.adjusted(1.5, 1.5, -1.5, -1.5))
        if on:
            p.fillRect(r.adjusted(6, 6, -6, -6), theme.pop())
        return
    ring = {"fade": theme.highlight(), "spring": theme.accent(), "glide": theme.pop(), "slam": theme.text(),
            "draw": line_color(theme)}[k]
    width = {"slam": 3.0, "draw": 1.2, "glide": 1.3}.get(k, 2.0)
    fill = theme.surface() if k != "slam" else (theme.highlight() if hover else theme.surface())
    if k in ("fade", "spring") and on:
        ring = theme.pop()
    p.setPen(QPen(ring if not hover or k == "slam" else ring.lighter(130), width))
    p.setBrush(fill if k not in ("glide", "draw") else Qt.NoBrush)
    p.drawEllipse(r.adjusted(width / 2, width / 2, -width / 2, -width / 2))
    if on:
        dot = {"slam": theme.pop(), "draw": theme.text()}.get(k, theme.pop())
        p.setPen(QPen(theme.text(), 2) if k == "slam" else Qt.NoPen)
        p.setBrush(dot)
        rad = r.width() / 2 - (6 if k != "slam" else 5)
        p.drawEllipse(r.center(), rad, rad)
        if k == "draw":                                   # crosshair through it
            p.setPen(QPen(theme.text(), 1))
            c = r.center()
            p.drawLine(QPointF(c.x() - r.width() / 2 - 3, c.y()), QPointF(c.x() + r.width() / 2 + 3, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - r.height() / 2 - 3), QPointF(c.x(), c.y() + r.height() / 2 + 3))


# ---------------------------------------------------------------------------- text fields
def paint_field(p: QPainter, rect: QRectF, theme: Theme, focus: bool, hover: bool = False,
                multiline: bool = False) -> None:
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(rect)
    if k == "fade":
        fill = theme.page_background().lighter(118 if hover else 100)       # sunk into the card
        p.fillPath(_rr(r.adjusted(1, 1, -1, -1), 9), fill)
        if focus:
            p.setPen(QPen(theme.pop(), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(_rr(r.adjusted(1, 1, -1, -1), 9))
        return
    if k == "spring":
        rad = (r.height() / 2) if not multiline else 18
        inner = r.adjusted(2, 2, -2, -2)
        if focus:
            p.fillPath(_rr(r, rad + 2), _alpha(theme.pop(), 0.22))
        p.fillPath(_rr(inner, rad), _mix(theme.page_background(), theme.surface(), 0.6).lighter(112))
        p.setPen(QPen(theme.pop() if focus else theme.accent(), 1.5 if focus else 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(_rr(inner, rad))
        return
    if k == "glide":                                      # written on a line
        if multiline:
            p.fillRect(r, _alpha(theme.surface(), 0.6))
            p.setPen(QPen(theme.pop() if focus else _alpha(theme.pop(), 0.35), 1))
            p.drawLine(QPointF(r.left() + 0.5, r.top()), QPointF(r.left() + 0.5, r.bottom()))
            p.setPen(QPen(_alpha(theme.muted(), 0.35), 0.8))
            p.drawLine(QPointF(r.left(), r.bottom() - 0.5), QPointF(r.right(), r.bottom() - 0.5))
            return
        p.setPen(QPen(theme.pop() if focus else _alpha(theme.muted(), 0.55 if not hover else 0.9),
                      1.5 if focus else 1))
        p.drawLine(QPointF(r.left(), r.bottom() - 1), QPointF(r.right(), r.bottom() - 1))
        return
    if k == "type":
        p.fillRect(r, theme.surface())
        p.setPen(QPen(theme.pop() if focus else line_color(theme), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if focus and not multiline:                       # a prompt mark at the left
            p.fillRect(QRectF(r.left() + 1, r.top() + 1, 3, r.height() - 2), theme.pop())
        return
    if k == "slam":
        bw = 3.0
        p.fillRect(r, QColor("#fffbd6") if focus else theme.surface())
        p.setPen(QPen(theme.text(), bw, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.setBrush(Qt.NoBrush)
        p.drawRect(r.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2))
        return
    if k == "step":                                       # bevelled, sunk into the page
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(r, theme.app_background())
        dark, light = _alpha(QColor("#000000"), 0.6), (theme.accent() if focus else theme.highlight())
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 3), dark)
        p.fillRect(QRectF(r.left(), r.top(), 3, r.height()), dark)
        p.fillRect(QRectF(r.left(), r.bottom() - 3, r.width(), 3), light)
        p.fillRect(QRectF(r.right() - 3, r.top(), 3, r.height()), light)
        return
    # draw (Blueprint): a dashed line to write on (a dashed frame for big boxes), solid while typing
    line = line_color(theme)
    pen = QPen(line if focus else _alpha(line, 0.6), 1, Qt.SolidLine if focus else Qt.DashLine)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if multiline:
        p.fillRect(r, _alpha(theme.app_background(), 0.35))
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
    else:
        y = r.bottom() - 1
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(line, 1))
        p.drawLine(QPointF(r.left(), y - 4), QPointF(r.left(), y))
        p.drawLine(QPointF(r.right() - 0.5, y - 4), QPointF(r.right() - 0.5, y))


def field_text_color(theme: Theme) -> QColor:
    k = kind(theme)
    if k in ("glide", "draw"):
        return theme.text()
    if k == "step":
        return theme.text()
    return contrast_text(theme.surface()) if k != "slam" else QColor("#000000")


# ---------------------------------------------------------------------------- buttons
def button_label(theme: Theme, text: str) -> str:
    k = kind(theme)
    if k == "type":
        return f"[ {text.lower()} ]"
    if k in ("slam", "draw", "step", "glide"):
        return text.upper()
    return text


def button_font(theme: Theme, base: QFont) -> QFont:
    k = kind(theme)
    f = QFont(base)
    if k == "glide":
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.4)
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        f.setWeight(QFont.Weight(600))
    elif k == "draw":
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
    elif k == "slam":
        f = theme.body_font(f.pointSizeF(), 800)
    elif k in ("spring", "fade"):
        f.setWeight(QFont.Weight(600))
    return f


def button_extra(theme: Theme) -> tuple[int, int]:
    """Extra (width, height) a themed button needs beyond its text."""
    k = kind(theme)
    return {"slam": (18, 8), "step": (14, 8), "spring": (18, 6), "glide": (16, 6), "draw": (16, 6),
            "type": (6, 2)}.get(k, (14, 6))


def paint_button(p: QPainter, rect: QRectF, theme: Theme, down: bool, hover: bool) -> QColor:
    """Draws the button's body; returns the text color. Text is drawn by the caller (in rect_for_text)."""
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(rect)
    if k == "fade":
        fill = theme.highlight() if not down else theme.highlight().darker(115)
        if hover and not down:
            fill = fill.lighter(112)
        p.fillPath(_rr(r, 10), fill)
        p.setPen(QPen(_alpha(QColor("#ffffff"), 0.08), 1))
        p.drawLine(QPointF(r.left() + 8, r.top() + 0.5), QPointF(r.right() - 8, r.top() + 0.5))
        return contrast_text(fill)
    if k == "spring":
        a = 0.45 if down else (0.30 if hover else 0.18)
        p.fillPath(_rr(r, r.height() / 2), _alpha(theme.pop(), a))
        return theme.pop().lighter(125) if not hover else QColor("#ffffff")
    if k == "glide":
        if hover or down:
            p.fillPath(_rr(r.adjusted(0.5, 0.5, -0.5, -0.5), 3), _alpha(theme.pop(), 0.22 if down else 0.12))
        p.setPen(QPen(theme.pop(), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(_rr(r.adjusted(0.5, 0.5, -0.5, -0.5), 3))
        return theme.pop()
    if k == "type":
        if hover or down:
            p.fillRect(r, theme.pop() if not down else theme.pop().darker(130))
            return QColor("#000000")
        return theme.text()
    if k == "slam":
        off = 0 if down else 4
        body = QRectF(r.x() + (4 - off), r.y() + (4 - off), r.width() - 4, r.height() - 4)
        if off:
            p.fillRect(body.translated(off, off), theme.text())
        p.fillRect(body, theme.pop() if not hover else theme.pop().lighter(108))
        p.setPen(QPen(theme.text(), 3, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        p.drawRect(body.adjusted(1.5, 1.5, -1.5, -1.5))
        return QColor("#000000")
    if k == "step":
        p.setRenderHint(QPainter.Antialiasing, False)
        off = 0 if down else 3
        body = QRectF(r.x() + (3 - off), r.y() + (3 - off), r.width() - 3, r.height() - 3)
        sh = theme.shadow()
        if off and sh:
            p.fillRect(body.translated(off, off), sh[1])
        fill = theme.accent() if not hover else theme.accent().lighter(115)
        p.fillRect(body, fill)
        p.fillRect(QRectF(body.left(), body.top(), body.width(), 2), _alpha(QColor("#ffffff"), 0.35))
        p.fillRect(QRectF(body.left(), body.bottom() - 2, body.width(), 2), _alpha(QColor("#000000"), 0.35))
        return contrast_text(fill)
    # draw (Blueprint)
    if hover or down:
        p.fillRect(r, _alpha(theme.text(), 0.25 if down else 0.12))
    p.setPen(QPen(line_color(theme), 1))
    p.setBrush(Qt.NoBrush)
    p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
    return theme.text()


def button_text_rect(theme: Theme, rect: QRectF, down: bool) -> QRectF:
    k = kind(theme)
    if k == "slam":
        off = 0 if down else 4
        return QRectF(rect.x() + (4 - off), rect.y() + (4 - off), rect.width() - 4, rect.height() - 4)
    if k == "step":
        off = 0 if down else 3
        return QRectF(rect.x() + (3 - off), rect.y() + (3 - off), rect.width() - 3, rect.height() - 3)
    return rect


# ---------------------------------------------------------------------------- sliders
def paint_slider(p: QPainter, track: QRectF, x: float, theme: Theme, hover: bool, down: bool) -> None:
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    cy = track.center().y()
    if k in ("fade", "spring"):
        h = 4.0 if k == "fade" else 8.0
        t = QRectF(track.left(), cy - h / 2, track.width(), h)
        p.fillPath(_rr(t, h / 2), theme.highlight() if k == "fade" else theme.accent())
        p.fillPath(_rr(QRectF(t.left(), t.top(), x - t.left(), h), h / 2), theme.pop())
        rad = (8.0 if k == "fade" else 10.0) + (1.5 if hover or down else 0)
        if k == "spring" and (hover or down):
            p.setBrush(_alpha(theme.pop(), 0.25))
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x, cy), rad + 5, rad + 5)
        p.setBrush(QColor("#ffffff"))
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(x, cy), rad, rad)
        return
    if k == "glide":                                      # a hairline and a brass diamond
        p.fillRect(QRectF(track.left(), cy - 0.5, track.width(), 1), _alpha(theme.muted(), 0.5))
        p.fillRect(QRectF(track.left(), cy - 0.75, x - track.left(), 1.5), theme.pop())
        s = 6.5 if not (hover or down) else 7.5
        d = QPainterPath()
        d.moveTo(x, cy - s)
        d.lineTo(x + s, cy)
        d.lineTo(x, cy + s)
        d.lineTo(x - s, cy)
        d.closeSubpath()
        p.fillPath(d, theme.pop())
        return
    if k == "type":                                       # [######------]
        n = max(4, int(track.width() // 9))
        cell = track.width() / n
        filled = round((x - track.left()) / cell)
        for i in range(n):
            c = QRectF(track.left() + i * cell + 1, cy - 5, cell - 2, 10)
            p.fillRect(c, theme.pop() if i < filled else _alpha(theme.muted(), 0.25))
        return
    if k == "slam":
        p.fillRect(QRectF(track.left(), cy - 3, track.width(), 6), theme.text())
        p.fillRect(QRectF(track.left(), cy - 3, x - track.left(), 6), theme.pop())
        p.setPen(QPen(theme.text(), 1))
        p.drawRect(QRectF(track.left(), cy - 3, track.width(), 6))
        knob = QRectF(x - 7, cy - 9, 14, 18)
        p.fillRect(knob, theme.pop() if not (hover or down) else theme.surface())
        p.setPen(QPen(theme.text(), 3))
        p.drawRect(knob)
        return
    if k == "step":
        p.setRenderHint(QPainter.Antialiasing, False)
        n = max(4, int(track.width() // 12))
        cell = track.width() / n
        filled = round((x - track.left()) / cell)
        for i in range(n):
            p.fillRect(QRectF(track.left() + i * cell + 1, cy - 4, cell - 2, 8),
                       theme.pop() if i < filled else theme.highlight())
        p.fillRect(QRectF(x - 5, cy - 8, 10, 16), theme.accent())
        return
    # draw (Blueprint): a scale with ticks and a triangle pointer
    line = line_color(theme)
    p.setPen(QPen(line, 1))
    p.drawLine(QPointF(track.left(), cy), QPointF(track.right(), cy))
    for i in range(11):
        tx = track.left() + track.width() * i / 10
        p.drawLine(QPointF(tx, cy), QPointF(tx, cy + (5 if i % 5 == 0 else 3)))
    p.setPen(QPen(theme.text(), 2))
    p.drawLine(QPointF(track.left(), cy), QPointF(x, cy))
    tri = QPainterPath()
    tri.moveTo(x, cy - 1)
    tri.lineTo(x - 6, cy - 10)
    tri.lineTo(x + 6, cy - 10)
    tri.closeSubpath()
    p.fillPath(tri, theme.text())


# ---------------------------------------------------------------------------- scroll bars
def paint_scrollbar(p: QPainter, track: QRectF, handle: QRectF | None, theme: Theme, down: bool) -> None:
    k = kind(theme)
    p.setRenderHint(QPainter.Antialiasing)
    if k in ("fade", "spring"):
        if handle is not None:
            h = handle.adjusted(2, 2, -2, -2) if k == "fade" else handle.adjusted(1, 1, -1, -1)
            c = theme.highlight() if k == "fade" else theme.accent().lighter(130)
            p.fillPath(_rr(h, min(h.width(), h.height()) / 2), c.darker(115) if down else c)
        return
    if k == "glide":
        p.fillRect(QRectF(track.center().x() - 0.5, track.top(), 1, track.height()), _alpha(theme.muted(), 0.3))
        if handle is not None:
            p.fillRect(QRectF(handle.center().x() - 1.5, handle.top(), 3, handle.height()), theme.pop())
        return
    if k == "type":
        p.fillRect(track, theme.surface())
        if handle is not None:
            p.fillRect(handle.adjusted(2, 0, -2, 0), theme.pop() if down else _alpha(theme.pop(), 0.6))
        return
    if k == "slam":
        p.fillRect(track, theme.surface())
        p.setPen(QPen(theme.text(), 2))
        p.drawRect(track.adjusted(1, 1, -1, -1))
        if handle is not None:
            p.fillRect(handle.adjusted(3, 3, -3, -3), theme.text())
        return
    if k == "step":
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(track, theme.app_background())
        if handle is not None:
            p.fillRect(handle.adjusted(2, 2, -2, -2), theme.accent() if not down else theme.pop())
        return
    p.setPen(QPen(_alpha(line_color(theme), 0.5), 1, Qt.DashLine))
    p.drawLine(QPointF(track.center().x(), track.top()), QPointF(track.center().x(), track.bottom()))
    if handle is not None:
        p.setPen(QPen(theme.text(), 1))
        p.setBrush(_alpha(theme.text(), 0.25 if down else 0.12))
        p.drawRect(handle.adjusted(2.5, 1.5, -2.5, -1.5))


# ---------------------------------------------------------------------------- combo boxes (QSS)
def combo_stylesheet(theme: Theme) -> str:
    k = kind(theme)
    text = field_text_color(theme).name()
    surface, accent, pop = theme.surface().name(), theme.accent().name(), theme.pop().name()
    page = theme.page_background().name()
    box = {
        "fade": f"background-color: {page}; border: none; "
                f"border-radius: 9px; padding: 5px 10px;",
        "spring": f"background-color: {surface}; border: 1px solid {accent}; border-radius: 14px; padding: 5px 14px;",
        "glide": f"background: transparent; border: none; border-bottom: 1px solid {theme.muted().name()}; "
                 f"border-radius: 0; padding: 4px 2px;",
        "type": f"background-color: {surface}; border: 1px solid {line_color(theme).name(QColor.HexArgb)}; "
                f"border-radius: 0; padding: 4px 8px;",
        "slam": f"background-color: {surface}; border: 3px solid {theme.text().name()}; border-radius: 0; "
                f"padding: 4px 8px; font-weight: 800;",
        "step": f"background-color: {theme.app_background().name()}; border: 3px solid {theme.highlight().name()}; "
                f"border-radius: 0; padding: 3px 8px;",
        "draw": f"background: transparent; border: none; border-bottom: 1px dashed {theme.text().name()}; "
                f"border-radius: 0; padding: 4px 2px;",
    }[k]
    focus = {"glide": f"border-bottom: 2px solid {pop};", "draw": f"border-bottom: 1px solid {pop};",
             "type": f"border-color: {pop};", "step": f"border-color: {accent};",
             "spring": f"border-color: {pop};", "fade": f"border: 1px solid {pop};",
             "slam": "background-color: #fffbd6;"}[k]
    popup_border = {"slam": f"3px solid {theme.text().name()}", "step": f"3px solid {accent}"}.get(
        k, f"1px solid {line_color(theme).name()}")
    sel_bg = pop if k in ("spring", "glide", "type", "slam", "draw") else theme.highlight().name()
    sel_fg = contrast_text(QColor(sel_bg)).name()
    return f"""
        QComboBox {{ {box} color: {text}; }}
        QComboBox:focus, QComboBox:on {{ {focus} }}
        QComboBox::drop-down {{ border: none; width: 18px; }}
        QComboBox QAbstractItemView {{
            background-color: {page}; color: {theme.text().name()}; border: {popup_border};
            selection-background-color: {sel_bg}; selection-color: {sel_fg}; outline: none; padding: 4px;
        }}
    """
