"""The window's frame: the agent sidebar, an agent's header and tabs, its overview, the settings
tabs and the theme picker. Everything here paints itself from the theme (themes.py) and moves the
theme's way (motion.py)."""
from __future__ import annotations

import math
import time
from dataclasses import asdict
from typing import Any, Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import motion
from .themes import AGENT_COLORS, LOOKS, Look, words
from .ui_kit import Theme, contrast_text, rounded_rect_path
from .widgets import Page, bulk, clear_layout, label, row as hrow

ACTIVE = ("listening", "transcript", "thinking", "researching", "asking", "watching", "responding")


def stage_word(stage: str | None, w: dict[str, str]) -> str:
    if not stage or stage == "idle":
        return w["idle"]
    if stage in ("listening", "transcript", "asking"):
        return w["listening"]
    if stage == "responding":
        return w["speaking"]
    if stage == "thinking":
        return w["thinking"]
    if stage in ACTIVE:
        return w["working"]
    return w["idle"]


def agent_color(agent: dict[str, Any], index: int) -> QColor:
    c = QColor(agent.get("indicator_color") or "")
    return c if c.isValid() else QColor(AGENT_COLORS[index % len(AGENT_COLORS)])


def _path(rect: QRectF, radius: float) -> QPainterPath:
    if radius > 0:
        return rounded_rect_path(rect, min(radius, rect.height() / 2, rect.width() / 2))
    p = QPainterPath()
    p.addRect(rect)
    return p


def paint_box(p: QPainter, rect: QRectF, theme: Theme, fill: QColor | None = None, border: QColor | None = None,
              radius: float | None = None) -> None:
    """A themed panel: hard shadow (Arcade), fill, outline."""
    r = theme.corner_radius() if radius is None else radius
    sh = theme.shadow()
    if sh:
        p.fillPath(_path(rect.translated(sh[0], sh[0]), r), sh[1])
    if fill is not None:
        p.fillPath(_path(rect, r), fill)
    if border is not None:
        p.setPen(QPen(border, theme.border_width))
        p.setBrush(Qt.NoBrush)
        p.drawPath(_path(rect, r))


def themed_menu(parent: QWidget) -> Any:
    """A right-click menu in the theme's look."""
    from PySide6.QtWidgets import QMenu

    from .ui_kit import skin
    t = Theme()
    k = skin.kind(t)
    m = QMenu(parent)
    m.setWindowFlags(m.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    m.setAttribute(Qt.WA_TranslucentBackground)
    radius = {"fade": 10, "spring": 16, "glide": 4}.get(k, 0)
    border = {"slam": f"3px solid {t.text().name()}", "step": f"3px solid {t.accent().name()}",
              "glide": f"1px solid {t.pop().name()}"}.get(k, f"1px solid {skin.line_color(t).name()}")
    bg = t.surface().name() if k not in ("type", "draw") else t.page_background().name()
    sel = t.pop() if k != "fade" else t.highlight()
    item_r = {"fade": 6, "spring": 10}.get(k, 0)
    m.setFont(t.body_font() if k != "type" else t.mono_font())
    m.setStyleSheet(
        f"QMenu {{ background: {bg}; color: {t.text().name()}; border: {border}; border-radius: {radius}px; "
        f"padding: 6px; }}"
        f"QMenu::item {{ padding: 6px 22px 6px 14px; border-radius: {item_r}px; }}"
        f"QMenu::item:selected {{ background: {sel.name()}; color: {contrast_text(sel).name()}; }}"
        f"QMenu::item:disabled {{ color: {t.muted().name()}; }}"
        f"QMenu::separator {{ height: 1px; background: {skin.line_color(t).name()}; margin: 5px 8px; }}"
        f"QMenu::indicator {{ width: 0; }}")
    return m


class Ticker(QTimer):
    """One shared clock for the little live animations (pulses, cursors, waveforms); it only runs
    while something asks for it."""

    def __init__(self, parent: QWidget, ms: int = 50) -> None:
        super().__init__(parent)
        self.setInterval(ms)
        self.t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self.t0


# ---------------------------------------------------------------------------- buttons
class PopButton(QAbstractButton):
    """The main button (Talk): the theme's signature color. Arcade's blinks."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.theme = Theme()
        self.setCursor(Qt.PointingHandCursor)
        self._on = True
        if self.theme.motion == "step":
            self._blink = QTimer(self)
            self._blink.setInterval(600)
            self._blink.timeout.connect(self._toggle)
            self._blink.start()

    def _toggle(self) -> None:
        self._on = not self._on
        self.update()

    def _font(self) -> QFont:
        t = self.theme
        if t.settings.display_upper or t.motion in ("type", "step"):
            return t.display_font(11)
        return t.body_font(weight=600)

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self._font())
        sh = (self.theme.shadow() or (0, None))[0]
        return QSize(int(fm.horizontalAdvance(self._label()) + 44 + sh), int(fm.height() + 24 + sh))

    def _label(self) -> str:
        text = self.text()
        if self.theme.motion == "type":
            return f"[ {text.lower()} ]"
        return self.theme.heading(text)

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        sh = (t.shadow() or (0, None))[0]
        rect = QRectF(0, 0, self.width() - sh, self.height() - sh)
        if not self.isEnabled():
            p.setOpacity(0.35)
        bg = QColor(t.pop())
        if self.isDown():
            bg = bg.darker(120)
        elif self.underMouse():
            bg = bg.lighter(110)
        radius = rect.height() / 2 if t.motion == "spring" else t.corner_radius()
        paint_box(p, rect, t, bg, None, radius)
        fg = contrast_text(bg)
        if t.motion == "step" and not self._on and not self.underMouse():
            fg.setAlpha(70)
        p.setPen(fg)
        p.setFont(self._font())
        p.drawText(rect, Qt.AlignCenter, self._label())
        p.end()

    def enterEvent(self, e: Any) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:
        self.update()
        super().leaveEvent(e)


# ---------------------------------------------------------------------------- tabs
class ThemedTabs(QWidget):
    """A row of tabs whose marker moves the theme's way: Ledger's underline glides, Alive's pill
    springs, Console's bracket jumps and blinks, Brutalist's block snaps, Arcade's steps, Blueprint's
    outline draws itself."""
    picked = Signal(str)

    def __init__(self, items: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = Theme()
        self.items = items
        self.current = items[0][0] if items else ""
        self.hover = ""
        self.marker: QRectF | None = None
        self.blink = 1.0
        self._anim: Any = None
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        fm = QFontMetricsF(self._font())
        self.setFixedHeight(int(fm.height() + (22 if self.theme.settings.tab_style == "underline" else 26)))

    def set_items(self, items: list[tuple[str, str]]) -> None:
        self.items = items
        self.marker = None
        self.updateGeometry()
        self.update()

    def _font(self) -> QFont:
        t = self.theme
        if t.settings.vocab == "ledger":
            return t.display_font(14, 600)
        return t.body_font(weight=600 if t.settings.tab_style in ("block", "pill") else 500)

    def _text(self, key: str, text: str) -> str:
        t = self.theme
        style = t.settings.tab_style
        if style == "brackets" and not t.settings.display_upper:
            text = text.lower()
        else:
            text = t.heading(text)
        if style == "brackets" and key == self.current:
            text = f"[{text}]"
        return text

    def _rects(self) -> list[tuple[str, QRectF]]:
        fm = QFontMetricsF(self._font())
        style = self.theme.settings.tab_style
        pad, gap = {"underline": (2, 26), "pill": (16, 8), "block": (16, 0), "brackets": (10, 4)}.get(style, (12, 8))
        x, out = 0.0, []
        h = self.height()
        for key, text in self.items:
            w = fm.horizontalAdvance(self._text(key, text) if key != self.current else
                                     self._text(key, text)) + 2 * pad
            if style == "brackets":                # room for the brackets either way
                w = fm.horizontalAdvance(f"[{self._text('', text)}]") + 2 * pad
            top = 0 if style in ("underline", "block") else 2
            out.append((key, QRectF(x, top, w, h - top - (0 if style in ("underline", "block") else 2))))
            x += w + gap
        return out

    def sizeHint(self) -> QSize:
        r = self._rects()
        return QSize(int(r[-1][1].right()) + 4 if r else 100, self.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(80, self.height())

    def _marker_for(self, key: str) -> QRectF | None:
        for k, r in self._rects():
            if k == key:
                if self.theme.settings.tab_style == "underline":
                    return QRectF(r.x(), r.bottom() - 3, r.width(), 3)
                return QRectF(r)
        return None

    def set_current(self, key: str, animate: bool = True) -> None:
        old = self.marker or self._marker_for(self.current)
        self.current = key
        new = self._marker_for(key)
        if new is None:
            return
        m = self.theme.motion
        if not animate or old is None or not self.isVisible():
            self.marker = new
            self.update()
            return
        if self._anim is not None:
            try:
                self._anim.stop()
            except RuntimeError:
                pass
        if m == "type":                       # jumps there, then blinks
            self.marker = new
            self._anim = motion.animate_value(self, 0.0, 1.0, m, self._set_blink, ms=360, steps=6)
        elif m == "draw":                     # the outline draws itself in from the left
            self._anim = motion.animate_value(self, QRectF(new.x(), new.y(), 0, new.height()), new, m,
                                              self._set_marker)
        else:
            self._anim = motion.animate_value(self, QRectF(old), new, m, self._set_marker)
        self.update()

    def _set_marker(self, r: Any) -> None:
        self.marker = QRectF(r)
        self.update()

    def _set_blink(self, v: Any) -> None:
        self.blink = 1.0 if int(float(v) * 6) % 2 == 0 or float(v) >= 1.0 else 0.0
        self.update()

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self.marker = self._marker_for(self.current)

    def mouseMoveEvent(self, e: Any) -> None:
        key = next((k for k, r in self._rects() if r.contains(e.position())), "")
        if key != self.hover:
            self.hover = key
            self.update()

    def leaveEvent(self, _e: Any) -> None:
        self.hover = ""
        self.update()

    def mousePressEvent(self, e: Any) -> None:
        key = next((k for k, r in self._rects() if r.contains(e.position())), "")
        if key and key != self.current:
            self.set_current(key)
            self.picked.emit(key)

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        style = t.settings.tab_style
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(self._font())
        text_c, muted, pop, accent = t.text(), t.muted(), t.pop(), t.accent()
        bg = t.page_background()
        if self.marker is None:
            self.marker = self._marker_for(self.current)
        rects = self._rects()
        if style == "underline":
            p.fillRect(QRectF(0, self.height() - 1, self.width(), 1), accent)
        for key, r in rects:                   # resting tabs
            if style == "pill":
                p.fillPath(_path(r, r.height() / 2), t.highlight())
            elif style == "block":
                if t.border_width >= 3 and t.motion == "slam":
                    p.setPen(QPen(text_c, 2))
                    p.drawLine(r.topRight(), r.bottomRight())
                elif t.motion == "step":
                    p.setPen(QPen(accent.darker(150), 2, Qt.DashLine))
                    p.drawRect(r.adjusted(1, 1, -1, -1))
            elif style == "brackets" and t.motion == "draw":
                p.setPen(QPen(text_c, 1))
                p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        m = self.marker
        if m is not None:                      # the marker
            if style == "underline":
                p.fillRect(QRectF(m.x(), self.height() - 3, m.width(), 2.5), pop)
            elif style == "pill":
                p.fillPath(_path(m, m.height() / 2), text_c)
            elif style == "block":
                p.fillRect(m, text_c if t.motion == "slam" else pop)
            elif style == "brackets":
                c = QColor(text_c)
                c.setAlphaF(self.blink)
                p.fillRect(m, c)
        for key, r in rects:                   # labels on top
            on = key == self.current
            if on and style == "pill":
                c = t.page_background()
            elif on and style == "block":
                c = bg if t.motion == "slam" else contrast_text(pop)
            elif on and style == "brackets":
                c = bg if self.blink > 0.5 else text_c
            elif on:
                c = text_c
            else:
                c = text_c if key == self.hover else muted
            if not on and style == "brackets" and t.motion == "draw":
                c = text_c
            p.setPen(c)
            text = dict(self.items)[key]
            p.drawText(r.adjusted(0, 0, 0, -3 if style == "underline" else 0), Qt.AlignCenter, self._text(key, text))
        p.end()


# ---------------------------------------------------------------------------- sidebar
class SideRow(QAbstractButton):
    """An agent (avatar, name, what it's doing) or a shared item in the sidebar."""

    def __init__(self, key: str, title: str, kind: str = "nav", color: QColor | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key, self.title, self.kind = key, title, kind
        self.color = color or QColor("#888888")
        self.theme = Theme()
        self.stage = ""
        self.pulse = 0.0
        self.on = True                       # an agent that's switched off is dimmed and says so
        self.main = False                    # the Main agent: a small star
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        fm = QFontMetricsF(self.theme.body_font())
        self.setFixedHeight(int(fm.height() * (2.6 if kind == "agent" else 1.9)) + 6)

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.update()

    def active(self) -> bool:
        return self.stage in ACTIVE

    def enterEvent(self, e: Any) -> None:            # repaint for the hover highlight (only the window's
        self.update()                                # first repaint used to show it)
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        s = t.settings
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        on = self.isChecked()
        text_c = t.text()
        if on:
            if s.motion == "type":
                p.fillRect(r, t.pop())
                text_c = contrast_text(t.pop())
            elif s.motion == "slam":
                p.fillRect(r, t.pop())
                text_c = QColor("#000000")
            elif s.motion == "step":
                p.fillRect(r, t.pop())
                text_c = contrast_text(t.pop())
            elif s.motion == "draw":
                c = QColor(255, 255, 255, 36)
                p.fillRect(r, c)
            else:
                p.fillPath(_path(r, min(t.corner_radius(), 14)), t.highlight())
        elif self.underMouse():
            c = QColor(t.highlight())
            c.setAlpha(110)
            p.fillPath(_path(r, min(t.corner_radius(), 14)), c)
        w = words(s.vocab)
        x = r.x() + 10
        if self.kind == "agent" and not self.on:
            p.setOpacity(0.5)
        if self.kind == "agent":
            av = r.height() - 14
            ar = QRectF(x, r.center().y() - av / 2, av, av)
            if s.motion in ("glide", "spring", "fade", "step"):
                self._avatar(p, ar, on)
                x = ar.right() + 12
            name_f = t.body_font(weight=600)
            p.setFont(name_f)
            p.setPen(text_c)
            fm = QFontMetricsF(name_f)
            prefix = "> " if (s.motion == "type" and on) else ("  " if s.motion == "type" else "")
            name = t.heading(self.title) if s.display_upper else self.title
            p.drawText(QPointF(x, r.center().y() - 2), prefix + name)
            if self.main:                      # the Main agent
                mx = x + fm.horizontalAdvance(prefix + name) + 6
                p.setFont(t.body_font(max(7.0, t.body_font().pointSizeF() - 2), 700))
                p.setPen(text_c if on and s.motion in ("type", "slam", "step") else t.pop())
                p.drawText(QPointF(mx, r.center().y() - 2), "MAIN" if s.motion in ("type", "slam", "step", "draw")
                           else "★")
                p.setFont(name_f)
            status = stage_word(self.stage, w) if self.on else w["off"]
            small = t.body_font(max(7.0, t.body_font().pointSizeF() - 1))
            p.setFont(small)
            sc = QColor(text_c if on and s.motion in ("type", "slam", "step") else t.muted())
            if self.active():
                sc = QColor(text_c) if on and s.motion in ("type", "slam", "step") else QColor(t.pop())
            p.setPen(sc)
            p.drawText(QPointF(x + (QFontMetricsF(name_f).horizontalAdvance(prefix)), r.center().y() + fm.height() - 4),
                       t.heading(status) if s.display_upper else status)
            if self.active():                  # the live dot: breathing, or blinking in steps
                k = 0.5 + 0.5 * math.sin(self.pulse * 5)
                if s.motion == "step":
                    k = 1.0 if int(self.pulse * 3) % 2 == 0 else 0.25
                d = QColor(self.color if s.motion == "spring" else t.pop())
                d.setAlphaF(0.35 + 0.65 * k)
                rad = 4 + 2 * k
                c = QPointF(r.right() - 14, r.center().y())
                if s.motion in ("slam", "draw", "type"):
                    p.fillRect(QRectF(c.x() - 4, c.y() - 4, 8, 8), text_c if on else d)
                else:
                    p.setBrush(d)
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(c, rad, rad)
        else:
            f = t.body_font()
            p.setFont(f)
            p.setPen(text_c if on else t.muted() if not self.underMouse() else t.text())
            title = t.heading(self.title) if s.display_upper else (self.title.lower() if s.motion == "type" else self.title)
            p.drawText(QRectF(x, r.y(), r.width() - x, r.height()), Qt.AlignVCenter | Qt.AlignLeft, title)
        p.end()

    def _avatar(self, p: QPainter, r: QRectF, on: bool) -> None:
        t = self.theme
        s = t.settings
        letter = (self.title[:1] or "?").upper()
        if s.motion == "spring":               # Alive: its own color, a ring while it's live
            fill = QColor(self.color) if on or self.active() else QColor(self.color).darker(260)
            p.fillPath(_path(r, r.width() * 0.32), fill)
            fg = contrast_text(fill) if on or self.active() else QColor(self.color)
            if self.active():
                ring = QColor(self.color)
                k = 0.5 + 0.5 * math.sin(self.pulse * 5)
                ring.setAlphaF(0.3 + 0.5 * k)
                p.setPen(QPen(ring, 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawPath(_path(r.adjusted(-4, -4, 4, 4), r.width() * 0.4))
        elif s.motion == "glide":              # Ledger: an engraved brass ring
            p.setPen(QPen(t.pop() if on else t.accent(), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r)
            fg = t.pop() if on else t.muted()
        elif s.motion == "step":               # Arcade: a square chip
            p.fillRect(r, t.accent() if not on else t.page_background())
            fg = t.text()
        else:
            p.setBrush(t.highlight() if not on else t.accent())
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
            fg = t.text()
        f = t.display_font(r.height() * 0.42)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(r, Qt.AlignCenter, letter)


class Sidebar(QWidget):
    """Agents first (picking one shows it), then what every agent shares."""
    chosen = Signal(str, str)        # ("agent", id) or ("nav", activity|models|settings)
    add_agent = Signal()
    switch_agent = Signal(str, bool)  # right-click > on/off
    talk_to = Signal(str)             # right-click > Talk
    make_main = Signal(str)           # right-click > Main agent ("" = none)
    lobotomize = Signal(str)          # right-click > Lobotomize
    set_mute = Signal(str, str, bool)  # right-click > Mute voice / Mute on screen: (agent, "audio"|"visual", on)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = Theme()
        self.w = words(self.theme.settings.vocab)
        self.rows: dict[tuple[str, str], SideRow] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.brand = BrandMark()
        lay.addWidget(self.brand)
        lay.addSpacing(6)
        lay.addWidget(self._caption(self.w["agents"]))
        self.agent_box = QWidget()
        self.agent_lay = QVBoxLayout(self.agent_box)
        self.agent_lay.setContentsMargins(0, 0, 0, 0)
        self.agent_lay.setSpacing(2)
        lay.addWidget(self.agent_box)
        self.new_btn = SideRow("new", self.w["new_agent"])
        self.new_btn.setCheckable(False)
        self.new_btn.clicked.connect(self.add_agent.emit)
        lay.addWidget(self.new_btn)
        lay.addStretch(1)
        lay.addWidget(self._caption(self.w["shared"]))
        for key in ("activity", "models", "settings"):
            row = SideRow(key, self.w[key])
            row.clicked.connect(lambda _c=False, k=key: self.pick("nav", k))
            self.rows[("nav", key)] = row
            lay.addWidget(row)
        self.footer = QVBoxLayout()
        lay.addLayout(self.footer)
        self.ticker = Ticker(self)
        self.ticker.timeout.connect(self._tick)

    def _caption(self, text: str) -> QWidget:
        t = self.theme
        lab = label(t.heading(text) if t.settings.display_upper else
                    (f"// {text.lower()}" if t.motion == "type" else text), False)
        f = t.display_font(13, 500) if t.settings.vocab == "ledger" else t.body_font(weight=600)
        if t.settings.vocab == "ledger":
            f.setItalic(True)
        lab.setFont(f)
        lab.setStyleSheet(f"QLabel {{ color: {t.muted().name()}; padding: 4px 8px 0 8px; }}")
        return lab

    def set_agents(self, agents: dict[str, dict[str, Any]], main: str = "") -> None:
        self.main = main
        for key in [k for k in self.rows if k[0] == "agent"]:
            self.rows.pop(key)
        with bulk(self.agent_box):
            clear_layout(self.agent_lay)
            for i, (aid, a) in enumerate(agents.items()):
                row = SideRow(aid, a.get("name") or aid, "agent", agent_color(a, i))
                row.on = bool(a.get("enabled", True))
                row.main = aid == main
                row.mute = dict(a.get("mute") or {})
                row.clicked.connect(lambda _c=False, k=aid: self.pick("agent", k))
                row.setContextMenuPolicy(Qt.CustomContextMenu)
                row.customContextMenuRequested.connect(lambda pos, r=row: self._menu(r, pos))
                self.rows[("agent", aid)] = row
                self.agent_lay.addWidget(row)

    def _menu(self, row: "SideRow", pos: Any) -> None:
        m = themed_menu(self)
        name = row.title
        talk = m.addAction(self.w["talk"].replace("{name}", name) if "{name}" in self.w["talk"]
                           else f"{self.w['talk']} — {name}")
        talk.setEnabled(row.on)
        talk.triggered.connect(lambda: self.talk_to.emit(row.key))
        m.addSeparator()
        flip = m.addAction(f"Turn {name} off" if row.on else f"Turn {name} on")
        flip.triggered.connect(lambda: self.switch_agent.emit(row.key, not row.on))
        main = m.addAction("Main agent ✓" if row.main else "Make main agent")
        main.setToolTip("Manual requests that don't name an agent go to the Main agent")
        main.triggered.connect(lambda: self.make_main.emit("" if row.main else row.key))
        m.addSeparator()
        mute = getattr(row, "mute", {}) or {}
        for kind, text in (("audio", "Mute voice"), ("visual", "Mute on screen")):
            act = m.addAction(("✓ " if mute.get(kind) else "") + text)
            act.triggered.connect(lambda _c=False, k=kind: self.set_mute.emit(row.key, k, not mute.get(k)))
        m.addSeparator()
        lob = m.addAction(f"Lobotomize {name}…")
        lob.triggered.connect(lambda: self.lobotomize.emit(row.key))
        m.addSeparator()
        show = m.addAction(f"Open {name}")
        show.triggered.connect(lambda: self.pick("agent", row.key))
        self._open_menu = m
        m.popup(row.mapToGlobal(pos))

    def set_on(self, aid: str, on: bool) -> None:
        row = self.rows.get(("agent", aid))
        if row is not None and row.on != on:
            row.on = on
            row.update()

    def pick(self, kind: str, key: str, emit: bool = True) -> None:
        for k, row in self.rows.items():
            row.setChecked(k == (kind, key))
        if emit:
            self.chosen.emit(kind, key)

    def set_stage(self, aid: str, stage: str) -> None:
        row = self.rows.get(("agent", aid))
        if row is not None:
            row.set_stage(stage)
        live = any(r.active() for r in self.rows.values())
        if live and not self.ticker.isActive():
            self.ticker.start()
        elif not live:
            self.ticker.stop()

    def _tick(self) -> None:
        now = self.ticker.now()
        for r in self.rows.values():
            if r.active():
                r.pulse = now
                r.update()


class BrandMark(QWidget):
    """'Jeeves' in the theme's display face, with the bow tie."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = Theme()
        self.setFixedHeight(int(QFontMetricsF(self.theme.display_font(18)).height() + 18))

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height()
        x = 10.0
        if t.motion not in ("type", "slam", "draw"):
            tie = QColor("#a8322b") if t.motion == "glide" else QColor(t.pop())
            cy, w2 = h / 2, 12
            path = QPainterPath()
            path.moveTo(x + w2, cy)
            path.lineTo(x, cy - 7)
            path.lineTo(x, cy + 7)
            path.closeSubpath()
            path.moveTo(x + w2, cy)
            path.lineTo(x + 2 * w2, cy - 7)
            path.lineTo(x + 2 * w2, cy + 7)
            path.closeSubpath()
            p.fillPath(path, tie)
            p.setBrush(t.pop() if t.motion == "glide" else tie)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x + w2, cy), 3, 3)
            x += 2 * w2 + 10
        f = t.display_font(18 if t.motion != "glide" else 21)
        if t.motion == "slam":
            f.setLetterSpacing(QFont.AbsoluteSpacing, -1)
        elif t.motion in ("type", "draw"):
            f.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        p.setFont(f)
        p.setPen(t.pop() if t.motion == "step" else t.text())
        name = "Jeeves" + ("." if t.settings.vocab == "" and t.motion == "fade" else "")
        p.drawText(QRectF(x, 0, self.width() - x, h), Qt.AlignVCenter | Qt.AlignLeft, t.heading(name))
        p.end()


# ---------------------------------------------------------------------------- an agent's header
class AgentHeader(QWidget):
    """The agent's name, what it's doing right now and the Talk button -- with the theme's life in it:
    Alive pulses and shows a waveform while the agent speaks; Console types its name and blinks a
    cursor; Ledger draws a brass rule under the name; Brutalist slams the name in; Arcade's sprite
    bobs; Blueprint's frame marches."""
    talk = Signal()
    switched = Signal(bool)          # the on/off switch beside Talk

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from .ui_kit import CustomCheckBox
        self.theme = Theme()
        s = self.theme.settings
        self.w = words(s.vocab)
        self.aid = ""
        self.on = True
        self.switch = CustomCheckBox(self.w["on_switch"], self)
        self.switch.setChecked(True)
        self.switch.setToolTip("Switch this agent on or off (also on its right-click menu in the list)")
        self.switch.toggled.connect(self._switched)
        self.agent: dict[str, Any] = {}
        self.index = 0
        self.color = QColor(self.theme.pop())
        self.stage = ""
        self.intro = 1.0
        self.button = PopButton(self.w["talk"].replace("{name}", "…"), self)
        self.button.clicked.connect(self.talk.emit)
        self.ticker = Ticker(self, 50)
        self.ticker.timeout.connect(self.update)
        big = {"slam": 64, "glide": 40, "draw": 40, "type": 34, "step": 24, "spring": 32}.get(self.theme.motion, 26)
        self.name_font = self.theme.display_font(big, 700 if self.theme.motion != "glide" else 600)
        fm = QFontMetricsF(self.name_font)
        self.setMinimumHeight(int(fm.height() + 70))
        self.bars = [0.3] * 18

    # -- state
    def set_agent(self, aid: str, agent: dict[str, Any], index: int) -> None:
        changed = aid != self.aid
        self.aid, self.agent, self.index = aid, agent, index
        self.color = agent_color(agent, index) if self.theme.motion == "spring" else QColor(self.theme.pop())
        name = agent.get("name") or aid
        self.button.setText(self.w["talk"].replace("{name}", name))
        self.button.updateGeometry()
        self.set_on(bool(agent.get("enabled", True)))
        if changed:
            self.intro = 0.0
            ms = {"type": 40 * max(4, len(name)) + 200}.get(self.theme.motion)
            motion.animate_value(self, 0.0, 1.0, self.theme.motion, self._set_intro, ms=ms)
            self._run()
        self.update()

    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self._run()
        self.update()

    def set_on(self, on: bool) -> None:
        self.on = on
        self.switch.blockSignals(True)
        self.switch.setChecked(on)
        self.switch.blockSignals(False)
        self.switch.setText(self.w["on_switch"] if on else self.w["off"])
        self.switch.updateGeometry()
        self.button.setEnabled(on)
        self._place_button()
        self.update()

    def _switched(self, on: bool) -> None:
        self.set_on(on)
        self.switched.emit(on)

    def _set_intro(self, v: Any) -> None:
        self.intro = float(v)
        self.update()

    def live(self) -> bool:
        return self.stage in ACTIVE

    def _run(self) -> None:
        """The clock runs while the agent is live, or for the theme's always-on touches."""
        always = self.theme.motion in ("type", "step", "draw")
        want = self.isVisible() and (self.live() or always or self.intro < 1.0)
        self.ticker.setInterval(50 if self.live() else {"type": 500, "step": 300, "draw": 90}.get(self.theme.motion, 50))
        if want and not self.ticker.isActive():
            self.ticker.start()
        elif not want:
            self.ticker.stop()

    def showEvent(self, e: Any) -> None:
        super().showEvent(e)
        self._run()

    def hideEvent(self, e: Any) -> None:
        super().hideEvent(e)
        self.ticker.stop()

    def resizeEvent(self, e: Any) -> None:
        super().resizeEvent(e)
        self._place_button()

    def _place_button(self) -> None:
        hint = self.button.sizeHint()
        self.button.setGeometry(self.width() - hint.width() - 6, (self.height() - hint.height()) // 2,
                                hint.width(), hint.height())
        sw = self.switch.sizeHint()
        self.switch.setGeometry(self.button.x() - sw.width() - 18, (self.height() - sw.height()) // 2,
                                sw.width(), sw.height())

    # -- painting
    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        s = t.settings
        m = t.motion
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = self.ticker.now()
        k = max(0.0, min(1.0, self.intro))
        h = self.height()
        x = 6.0
        right = self.switch.x() - 20
        name = self.agent.get("name") or self.aid or ""
        if s.display_upper:
            name = name.upper()
        status = stage_word(self.stage, self.w) if self.on else self.w["off"]
        if not self.on:
            p.setOpacity(0.55)
        calls = ", ".join(self.agent.get("call_names") or [])

        if m == "spring":                      # Alive: a colored panel with the agent's own color
            panel = QRectF(0, 4, self.width(), h - 8)
            tint = QColor(self.color)
            tint.setAlpha(46)
            p.fillPath(_path(panel, 24), tint)
            x = 26.0
        if m in ("glide", "spring", "fade", "step"):
            size = h - (44 if m != "step" else 38)
            ar = QRectF(x, (h - size) / 2, size, size)
            if m == "step":                    # the sprite bobs in two-pixel steps
                ar.translate(0, -3 if int(now * 3.3) % 2 == 0 else 0)
            elif m == "spring":
                sc = 0.6 + 0.4 * k
                ar = QRectF(ar.center().x() - size * sc / 2, ar.center().y() - size * sc / 2, size * sc, size * sc)
            self._avatar(p, ar, now)
            x += size + 22

        # the caption above the name
        cap = {"glide": f"AGENT NO. {self.index + 1}", "type": f"agent/{self.aid}",
               "draw": f"AGENT A{self.index + 1} — GENERAL ARRANGEMENT",
               "step": "CLASS: " + ((self.agent.get("prompt") or "agent").split(".")[0].split(",")[0][:24].upper()),
               }.get(m, "")
        nfm = QFontMetricsF(self.name_font)
        y_name = h / 2 + nfm.ascent() / 2 - (6 if cap else 10)
        if cap:
            cf = t.body_font(max(7.0, t.body_font().pointSizeF() - 1.5), 600)
            if m == "glide":
                cf.setLetterSpacing(QFont.AbsoluteSpacing, 3)
            p.setFont(cf)
            p.setPen(t.pop() if m == "glide" else t.muted())
            p.drawText(QPointF(x, y_name - nfm.ascent() - (14 if m == "draw" else 4)), cap)

        # the name, entering the theme's way
        p.setFont(self.name_font)
        shown = name
        nx = x
        if m == "type":
            shown = name[:int(len(name) * k + 0.999)]
        elif m == "slam":
            nx = x + 120 * (1 - k)
        elif m == "glide":
            pass
        c = QColor(t.pop() if m == "step" else t.text())
        if m in ("glide", "fade"):
            c.setAlphaF(k)
        if m == "step":                        # pixel title with its shadow
            p.setPen(QColor(s.color_accent))
            p.drawText(QPointF(nx + 3, y_name + 3), shown)
        p.setPen(c)
        p.drawText(QPointF(nx, y_name + (10 * (1 - k) if m == "glide" else 0)), shown)
        name_w = nfm.horizontalAdvance(shown)
        if m == "type":                        # the cursor
            on = int(now * 2) % 2 == 0 or k < 1.0
            if on:
                p.fillRect(QRectF(nx + name_w + 4, y_name - nfm.ascent() * 0.8, nfm.averageCharWidth() * 0.7,
                                  nfm.ascent() * 0.85), t.pop())
        if m == "glide":                       # a brass rule draws itself under the name
            p.fillRect(QRectF(nx, y_name + 8, name_w * k, 1.5), t.pop())
        if m == "draw":                        # marching dimension frame round the name
            frame = QRectF(nx - 8, y_name - nfm.ascent() - 6, name_w + 16, nfm.ascent() + 14)
            pen = QPen(t.text(), 1, Qt.DashLine)
            pen.setDashOffset(-now * 12)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(frame.x(), frame.y(), frame.width() * k, frame.height()))

        # the line under it: what it's doing, and what it answers to
        sf = t.body_font()
        p.setFont(sf)
        sfm = QFontMetricsF(sf)
        y2 = y_name + sfm.height() + (14 if m == "glide" else 8)
        live_c = QColor(self.color if m == "spring" else t.pop())
        if m == "slam" and self.live():
            on = int(now * 4) % 2 == 0
            badge = QRectF(nx, y2 - sfm.ascent() - 3, sfm.horizontalAdvance(status.upper()) + 14, sfm.height() + 4)
            p.fillRect(badge, t.text() if on else t.pop())
            p.setPen(t.page_background() if on else QColor("#000000"))
            p.drawText(badge, Qt.AlignCenter, status.upper())
            sx = badge.right() + 12
        else:
            if self.live():
                p.setPen(live_c)
            else:
                p.setPen(t.muted())
            st = status.upper() if s.display_upper else status
            if m == "type":
                st = f"status: {st.lower()}"
            p.drawText(QPointF(nx, y2), st)
            sx = nx + sfm.horizontalAdvance(st) + 14
        if calls:
            p.setPen(t.muted())
            tail = (f"· answers to {calls}" if m != "type" else f"· call_names: {calls}")
            if s.display_upper:
                tail = tail.upper()
            p.drawText(QPointF(sx, y2), sfm.elidedText(tail, Qt.ElideRight, max(10.0, right - sx)))

        if self.live() and m in ("spring", "type", "fade", "step"):    # a waveform while it's live
            self._wave(p, QRectF(max(nx + name_w + 40, right - 260), h / 2 - 34, 220, 52), now, live_c)
        p.end()

    def _wave(self, p: QPainter, r: QRectF, now: float, color: QColor) -> None:
        m = self.theme.motion
        n = len(self.bars)
        speaking = self.stage == "responding"
        bw = r.width() / n
        for i in range(n):
            amp = (0.55 + 0.45 * math.sin(now * 9 + i * 0.8) * math.sin(now * 3.1 + i * 1.7)) if speaking else \
                0.18 + 0.12 * math.sin(now * 4 + i * 0.6)
            if m == "step":
                amp = round(amp * 4) / 4
            hgt = max(4.0, r.height() * abs(amp))
            bar = QRectF(r.x() + i * bw + bw * 0.2, r.center().y() - hgt / 2, bw * 0.6, hgt)
            if m == "type":
                bar = QRectF(bar.x(), r.bottom() - hgt, bar.width(), hgt)
                p.fillRect(bar, color)
            else:
                c = QColor(color)
                c.setAlphaF(0.45 + 0.55 * abs(amp))
                p.fillPath(_path(bar, bar.width() / 2 if m != "step" else 0), c)

    def _avatar(self, p: QPainter, r: QRectF, now: float) -> None:
        t = self.theme
        m = t.motion
        letter = ((self.agent.get("name") or self.aid or "?")[:1]).upper()
        if m == "step":                        # an 8x8 pixel face
            p.fillRect(r, QColor(t.settings.color_accent))
            cell = r.width() / 8
            face = ["        ", " ##  ## ", " ##  ## ", "        ", "#      #", " ###### ", "        ", "        "]
            if self.live() and int(now * 4) % 2 == 0:
                face[5] = "  ####  "
            for yy, line in enumerate(face):
                for xx, ch in enumerate(line):
                    if ch == "#":
                        p.fillRect(QRectF(r.x() + xx * cell, r.y() + yy * cell, cell + 0.5, cell + 0.5),
                                   t.page_background())
            return
        if m == "spring":
            if self.live():
                k = 0.5 + 0.5 * math.sin(now * 5)
                halo = QColor(self.color)
                halo.setAlphaF(0.25 * k + 0.1)
                p.fillPath(_path(r.adjusted(-10 * k - 4, -10 * k - 4, 10 * k + 4, 10 * k + 4), r.width() * 0.45), halo)
            p.fillPath(_path(r, r.width() * 0.32), self.color)
            fg = contrast_text(self.color)
        elif m == "glide":
            p.setPen(QPen(t.pop(), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(r)
            p.drawEllipse(r.adjusted(4, 4, -4, -4))
            fg = t.pop()
        else:
            p.setBrush(t.highlight())
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
            fg = t.text()
        p.setFont(t.display_font(r.height() * 0.36))
        p.setPen(fg)
        p.drawText(r, Qt.AlignCenter, letter)


# ---------------------------------------------------------------------------- an agent's overview
class Card(QAbstractButton):
    """A clickable summary (Voice, Brain, ...): caption, a big value, a hint."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = Theme()
        self.caption = caption
        self.value = ""
        self.hint = ""
        self.reveal = 1.0
        self.setCursor(Qt.PointingHandCursor)
        f1 = QFontMetricsF(self.theme.display_font(18, 600))
        f2 = QFontMetricsF(self.theme.body_font())
        sh = (self.theme.shadow() or (0, None))[0]
        self.setMinimumHeight(int(f1.height() + 2 * f2.height() + 40 + sh))

    def set(self, value: str, hint: str) -> None:
        self.value, self.hint = value, hint
        self.update()

    def play_in(self, delay: int) -> None:
        """Arrive the theme's way, drawn by the card itself (no graphics effect: those can't nest)."""
        self.reveal = 0.0
        self.update()
        QTimer.singleShot(delay, lambda: motion.animate_value(self, 0.0, 1.0, self.theme.motion, self._set_reveal))

    def _set_reveal(self, v: Any) -> None:
        self.reveal = float(v)
        self.update()

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        m = t.motion
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        sh = (t.shadow() or (0, None))[0]
        r = QRectF(self.rect()).adjusted(1, 1, -1 - sh, -1 - sh)
        k = self.reveal
        if k < 1.0:                             # arriving: each theme its own way
            if m in ("glide", "spring", "fade"):
                p.translate(0, (1 - k) * 24)
                p.setOpacity(max(0.0, min(1.0, k * 1.4)))
            elif m == "slam":
                p.translate((1 - k) * 60, 0)
            elif m == "step":
                p.setOpacity(k)
            elif m in ("type", "draw"):
                p.setClipRect(QRectF(0, 0, self.width() * k, self.height()) if m == "draw" else
                              QRectF(0, 0, self.width(), self.height() * k))
        fill = t.surface()
        if self.underMouse():
            fill = fill.lighter(118) if fill.lightness() < 128 else fill.darker(106)
        if m == "glide":                        # Ledger: ruled, no box
            p.setPen(QPen(t.accent(), 1))
            p.drawLine(r.topLeft(), r.topRight())
            p.drawLine(r.bottomLeft(), r.bottomRight())
        elif m == "draw":
            p.setPen(QPen(t.text(), 1.5))
            p.drawRect(r)
        else:
            paint_box(p, r, t, fill, t.accent() if m in ("slam", "step", "type") else None)
        pad = 18
        inner = r.adjusted(pad, 12, -pad, -12)
        cf = t.body_font(max(7.0, t.body_font().pointSizeF() - 1.5), 600)
        if m == "glide":
            cf.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        p.setFont(cf)
        p.setPen(t.pop() if m in ("glide", "draw") else t.muted())
        cap = self.caption.upper() if (t.settings.display_upper or m == "glide") else self.caption
        if m == "type":
            cap = self.caption.lower().replace(" ", "_")
        cfm = QFontMetricsF(cf)
        p.drawText(QPointF(inner.x(), inner.y() + cfm.ascent()), cap)
        vf = t.display_font(18 if m != "glide" else 21, 600)
        p.setFont(vf)
        p.setPen(t.text())
        vfm = QFontMetricsF(vf)
        val = t.heading(self.value) if t.settings.display_upper else self.value
        p.drawText(QPointF(inner.x(), inner.y() + cfm.height() + 6 + vfm.ascent()),
                   vfm.elidedText(val, Qt.ElideRight, inner.width()))
        hf = t.body_font()
        p.setFont(hf)
        p.setPen(t.muted())
        hfm = QFontMetricsF(hf)
        p.drawText(QPointF(inner.x(), inner.y() + cfm.height() + vfm.height() + 10 + hfm.ascent()),
                   hfm.elidedText(self.hint, Qt.ElideRight, inner.width()))
        p.end()

    def enterEvent(self, e: Any) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:
        self.update()
        super().leaveEvent(e)


class OverviewPage(Page):
    """An agent at a glance: what it sounds like, what it thinks with, what it can do, what it answers
    to (each opens its tab), and what it did lately."""
    open_tab = Signal(str)

    def __init__(self, daemon: Any) -> None:
        super().__init__("Overview", "", show_heading=False)
        self.daemon = daemon
        w = words(self._theme.settings.vocab)
        grid = QGridLayout()
        grid.setSpacing(self._theme.padding)
        self.cards: dict[str, Card] = {}
        for i, (key, cap) in enumerate((("voice", w["voice"]), ("brain", w["brain"]),
                                        ("abilities", w["abilities"]), ("personality", "Answers to"))):
            c = Card(cap)
            c.clicked.connect(lambda _c=False, k=key: self.open_tab.emit(k))
            self.cards[key] = c
            grid.addWidget(c, 0, i)
        self.body_layout.addLayout(grid)
        self.recent_title = QWidget()
        rt = QVBoxLayout(self.recent_title)
        rt.setContentsMargins(0, 8, 0, 0)
        head = label(self._theme.heading(w["recent"]), False)
        head.setFont(self._theme.display_font(14, 600))
        rt.addWidget(head)
        self.body_layout.addWidget(self.recent_title)
        self.recent = QWidget()
        self.recent_lay = QVBoxLayout(self.recent)
        self.recent_lay.setContentsMargins(0, 0, 0, 0)
        self.recent_lay.setSpacing(0)
        self.body_layout.addWidget(self.recent)
        self.finish()
        self.aid = ""

    def show_agent(self, aid: str, agent: dict[str, Any], settings: dict[str, Any], catalog: list[dict[str, Any]],
                   functions: list[dict[str, Any]], animate: bool) -> None:
        names = {m["id"]: m["name"] for m in catalog}
        models = agent.get("models") or {}
        glob = settings.get("models") or {}
        voice_id = models.get("tts_voice") or glob.get("tts_voice")
        style = agent.get("voice_style") or {}
        self.cards["voice"].set(names.get(voice_id, voice_id or "default voice"),
                                f"{float(style.get('speed', 1.0)):.1f}× · {style.get('effect', 'none')} effect"
                                if style.get("effect", "none") != "none" else f"speed {float(style.get('speed', 1.0)):.1f}×")
        brain = models.get("local_response") or (glob.get("local_response") or {}).get("model")
        intent = models.get("intent") or (glob.get("intent") or {}).get("model")
        self.cards["brain"].set(names.get(brain, brain or "—"), f"understands with {names.get(intent, intent or '—')}")
        per = agent.get("functions") or {}
        on = [f for f in functions if per.get(f["name"], f.get("globally_enabled", True))]
        some = ", ".join(f["title"] for f in on[:3])
        rud = agent.get("rudimentary") or {}
        if rud.get("enabled") and rud.get("function"):     # one function: only how to run it
            title = "Run commands" if rud["function"] == "run_what_i_say" else \
                next((f["title"] for f in functions if f["name"] == rud["function"]), rud["function"])
            self.cards["abilities"].set(title, "rudimentary: its only function")
            self.cards["brain"].set(names.get(intent, intent or "—"), f"just fills in {title}")
        else:
            self.cards["abilities"].set(f"{len(on)} on" if functions else "…", some + ("…" if len(on) > 3 else ""))
        calls = ", ".join(agent.get("call_names") or [])
        hear = {"user": "your microphone", "desktop": "desktop audio", "both": "mic and desktop",
                "device": "a device"}.get(agent.get("listen_to", "user"), "your microphone")
        self.cards["personality"].set(f"“{calls}”" if calls else "—", f"hears {hear}")
        if aid != self.aid or animate:
            self.aid = aid
            for i, c in enumerate(self.cards.values()):
                c.play_in(80 + i * 70)
            self.daemon.call("history.list", self._got, lambda _e: None, limit=8, agent=aid)

    def refresh_recent(self) -> None:
        if self.aid:
            self.daemon.call("history.list", self._got, lambda _e: None, limit=8, agent=self.aid)

    def _got(self, items: Any) -> None:
        fill_history(self.recent, self.recent_lay, items or [], self._theme, getattr(self, "daemon", None))
        self.relayout()


class _HistoryRow(QWidget):
    """A past request; clicked, it opens to show everything about it (as the Activity page does)."""

    def __init__(self, entry: dict[str, Any], lay: QVBoxLayout, daemon: Any = None) -> None:
        super().__init__()
        self.entry, self.lay, self.daemon = entry, lay, daemon
        self.detail: QWidget | None = None
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to see what happened")

    def mousePressEvent(self, ev: Any) -> None:
        if ev.button() == Qt.LeftButton:
            ev.accept()                       # (or the release never comes here: the labels on it don't take it)
            return
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev: Any) -> None:
        if ev.button() != Qt.LeftButton or not self.rect().contains(ev.position().toPoint()):
            return super().mouseReleaseEvent(ev)
        if self.detail is not None:
            self.detail.deleteLater()
            self.detail = None
        else:
            from .pages_misc import entry_details
            from .ui_kit import CustomButton, CustomPlainTextEdit
            box = QWidget()
            v = QVBoxLayout(box)
            v.setContentsMargins(4, 0, 4, 10)
            text = CustomPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText(entry_details(self.entry))
            text.setMinimumHeight(260)
            v.addWidget(text)
            if self.entry.get("sources") and self.daemon is not None:
                src = CustomButton("Show sources")
                src.clicked.connect(lambda: self.daemon.call("ui.popup", None, None, kind="sources",
                                                             data={"request": self.entry["id"]}))
                v.addWidget(src, alignment=Qt.AlignLeft)
            self.lay.insertWidget(self.lay.indexOf(self) + 1, box)
            self.detail = box
        w = self.parentWidget()
        while w is not None and not isinstance(w, Page):
            w = w.parentWidget()
        if w is not None:
            w.relayout()                          # the page grows (or shrinks) to fit it


def fill_history(box: QWidget, lay: QVBoxLayout, items: list[dict[str, Any]], theme: Theme,
                 daemon: Any = None) -> None:
    """Rows of past requests: when, what was said, what came of it -- click one for the whole of it."""
    m = theme.motion
    with bulk(box):
        clear_layout(lay)
        if not items:
            lay.addWidget(label("Nothing yet.", False))
        for e in items:
            row = _HistoryRow(e, lay, daemon)
            h = QHBoxLayout(row)
            h.setContentsMargins(4, 9, 4, 9)
            when = label(time.strftime("%H:%M", time.localtime(e.get("time", 0))), False)
            when.setStyleSheet(f"QLabel {{ color: {theme.muted().name()}; }}")
            when.setFixedWidth(64)
            text = label(theme.heading(e.get("text") or "") if theme.settings.display_upper else (e.get("text") or ""))
            result = f"{e.get('function') or '—'} · {e.get('status') or ''}"
            if theme.settings.vocab == "ledger":
                result = "Done, sir." if e.get("status") == "done" else (e.get("status") or "")
            elif theme.settings.vocab == "arcade":
                result = "CLEAR" if e.get("status") == "done" else (e.get("status") or "").upper()
            text.setStyleSheet(f"QLabel {{ color: {theme.text().name()}; }}")
            res = label(result, False)
            res.setStyleSheet(f"QLabel {{ color: {(theme.pop() if m in ('glide', 'step') else theme.muted()).name()}; }}")
            if m == "glide":
                res.setFont(theme.display_font(13, 500))
            h.addWidget(when)
            h.addWidget(text, 1)
            h.addWidget(res)
            row.setStyleSheet(f"QWidget#hrow {{ border-top: 1px {'dotted' if m == 'glide' else 'solid'} "
                              f"{theme.accent().name()}; }}")
            row.setObjectName("hrow")
            row.setAttribute(Qt.WA_StyledBackground, True)
            lay.addWidget(row)


class AgentHistoryPage(Page):
    """Everything one agent was asked."""

    def __init__(self, daemon: Any) -> None:
        super().__init__("History", "", show_heading=False)
        self.daemon = daemon
        self.aid = ""
        self.box = QWidget()
        self.lay = QVBoxLayout(self.box)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.body_layout.addWidget(self.box)
        self.finish()

    def show_agent(self, aid: str) -> None:
        self.aid = aid
        self.daemon.call("history.list", self._got, lambda _e: None, limit=60, agent=aid)

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        if self.aid:
            self.show_agent(self.aid)

    def _got(self, items: Any) -> None:
        self.after_fade(lambda: (fill_history(self.box, self.lay, items or [], self._theme, self.daemon),
                                 self.relayout()))


# ---------------------------------------------------------------------------- the theme picker
class LookCard(QAbstractButton):
    """A theme, previewed in its own colors and faces."""

    def __init__(self, look: Look, current: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.look = look
        self.s = look.settings()
        self.current = current
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(250, 170)
        self.hover = 0.0
        self.setCheckable(True)
        self.setChecked(current)

    def paintEvent(self, _e: Any) -> None:
        s = self.s
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3 - s.shadow_offset, -3 - s.shadow_offset)
        radius = min(s.rounded_corner_radius, 18) if s.rounded_corners_enabled else 0
        if s.shadow_offset:
            p.fillPath(_path(r.translated(s.shadow_offset, s.shadow_offset), radius), QColor(s.color_shadow))
        p.fillPath(_path(r, radius), QColor(s.color_page_background))
        if s.pattern == "grid":
            p.save()
            p.setClipPath(_path(r, radius))
            p.setPen(QColor(255, 255, 255, 26))
            for xx in range(int(r.x()), int(r.right()), 14):
                p.drawLine(xx, int(r.y()), xx, int(r.bottom()))
            for yy in range(int(r.y()), int(r.bottom()), 14):
                p.drawLine(int(r.x()), yy, int(r.right()), yy)
            p.restore()
        elif s.pattern == "scanlines":
            p.save()
            p.setClipPath(_path(r, radius))
            p.setPen(QColor(255, 255, 255, 14))
            for yy in range(int(r.y()), int(r.bottom()), 4):
                p.drawLine(int(r.x()), yy, int(r.right()), yy)
            p.restore()
        border = QColor(s.color_pop if self.isChecked() else s.color_accent)
        p.setPen(QPen(border, max(1.5, s.border_width) if not self.isChecked() else 3))
        p.setBrush(Qt.NoBrush)
        p.drawPath(_path(r, radius))
        inner = r.adjusted(18, 14, -18, -14)
        df = QFont(s.font_display or s.font_body or self.font().family())
        df.setPointSizeF(15 * (0.6 if s.font_display == "Press Start 2P" else 1.0))
        from .ui_kit.theme import set_weight
        set_weight(df, 700)
        p.setFont(df)
        p.setPen(QColor(s.color_text))
        name = self.look.name.upper() if s.display_upper else self.look.name
        p.drawText(QRectF(inner.x(), inner.y(), inner.width(), 30), Qt.AlignLeft | Qt.AlignVCenter, name)
        # a little tab row and a pop button, as the theme draws them
        y = inner.y() + 40
        bf = QFont(s.font_body or self.font().family())
        bf.setPointSizeF((s.font_size or 10) * 0.9)
        p.setFont(bf)
        tab = QRectF(inner.x(), y, 74, 24)
        pop = QColor(s.color_pop)
        if s.tab_style == "pill":
            p.fillPath(_path(tab, 12), QColor(s.color_text))
            p.setPen(QColor(s.color_page_background))
        elif s.tab_style in ("block", "brackets"):
            p.fillRect(tab, QColor(s.color_text) if s.motion in ("slam", "type", "draw") else pop)
            p.setPen(QColor(s.color_page_background) if s.motion in ("slam", "type", "draw") else contrast_text(pop))
        else:
            p.fillRect(QRectF(tab.x(), tab.bottom() - 2, tab.width(), 2), pop)
            p.setPen(QColor(s.color_text))
        p.drawText(tab, Qt.AlignCenter, "Overview" if not s.display_upper else "OVERVIEW")
        btn = QRectF(inner.right() - 70, y, 70, 24)
        p.fillPath(_path(btn, 12 if s.motion == "spring" else min(4, radius)), pop)
        p.setPen(contrast_text(pop))
        p.drawText(btn, Qt.AlignCenter, "Talk" if not s.display_upper else "TALK")
        p.setPen(QColor(s.color_muted))
        p.drawText(QRectF(inner.x(), y + 34, inner.width(), inner.bottom() - y - 34),
                   Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, self.look.blurb)
        p.end()


class LookPage(Page):
    """Pick a theme (the window rebuilds in it), then fine-tune colors and shapes below."""

    def __init__(self, daemon: Any, apply: Callable[[], None]) -> None:
        super().__init__("Look", "Pick a theme. Each has its own colors, typefaces and way of moving; the window "
                                 "redraws in it straight away. Fine-tune it below.")
        from .ui_kit import CustomButton, ThemeEditorGroup, get_settings
        from .common import JEEVES_THEME_DEFAULTS
        self.daemon, self.apply = daemon, apply
        grid = QGridLayout()
        grid.setSpacing(self._theme.padding)
        cur = get_settings().preset or "classic"
        self.cards: list[LookCard] = []
        for i, look in enumerate(LOOKS.values()):
            card = LookCard(look, look.key == cur)
            card.clicked.connect(lambda _c=False, lk=look: self.pick(lk))
            self.cards.append(card)
            grid.addWidget(card, i // 3, i % 3)
        self.body_layout.addLayout(grid)
        from .ui_kit import CustomCheckBox
        self.frame = CustomCheckBox("Rounded window: Jeeves draws its own frame and title bar (off: your desktop's)")
        self.frame.setChecked(get_settings().own_frame)
        self.frame.toggled.connect(self._frame)
        self.body_layout.addWidget(self.frame)
        self.editor = ThemeEditorGroup(current=get_settings(), defaults=JEEVES_THEME_DEFAULTS)
        self.body_layout.addWidget(self.editor)
        save = CustomButton("Save and apply")
        save.clicked.connect(self.save)
        self.look_note = QLabel("")
        self.body_layout.addWidget(hrow(self.look_note, save, stretch=0), alignment=Qt.AlignRight)
        # saved as you go -- without re-theming the window under you each time; "Save and apply" shows it
        from .autosave import AutoSave
        self.autosave = AutoSave(self.editor, state=self._target, save=self._save_quiet)
        self.autosave.loaded()
        self.finish()

    def _target(self) -> Any:
        from .ui_kit import ThemeSettings, get_settings
        target = ThemeSettings(**asdict(get_settings()))
        self.editor.apply_to(target)
        return asdict(target)

    def _save_quiet(self) -> None:
        import time
        self.daemon.call("settings.set", lambda _r: self.look_note.setText(
            time.strftime("Saved %H:%M:%S -- \"Save and apply\" to see it everywhere")), self._failed,
            changes={"theme": self._target()})

    def pick(self, look: Look) -> None:
        from .ui_kit import get_settings
        for c in self.cards:
            c.setChecked(c.look is look)
        target = look.settings()
        target.custom_widgets_enabled = get_settings().custom_widgets_enabled
        target.own_frame = get_settings().own_frame
        self.daemon.call("settings.set", lambda _r: self.apply(), self._failed, changes={"theme": asdict(target)})

    def _frame(self, on: bool) -> None:
        from .ui_kit import ThemeSettings, get_settings
        target = ThemeSettings(**asdict(get_settings()))
        target.own_frame = on
        self.daemon.call("settings.set", lambda _r: self.apply(), self._failed, changes={"theme": asdict(target)})

    def save(self) -> None:
        from .ui_kit import ThemeSettings, get_settings
        self.autosave.timer.stop()
        target = ThemeSettings(**asdict(get_settings()))
        bad = self.editor.apply_to(target)
        if bad:
            self._failed("Ignored invalid colors: " + ", ".join(bad))
        self.daemon.call("settings.set", lambda _r: self.apply(), self._failed, changes={"theme": asdict(target)})

    def _failed(self, err: str) -> None:
        from .ui_kit import show_message
        show_message(self, "Look", str(err))


# ---------------------------------------------------------------------------- the window's own frame
EDGE = 7


class WindowFrame(QWidget):
    """The window's background. With its own frame (Look > Rounded window) the window has no system
    frame: this paints its rounded (Arcade: pixel-stepped) outline and lets its edges resize it."""

    def __init__(self, own: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = Theme()
        self.own = own
        self.setMouseTracking(own)

    def _full(self) -> bool:
        w = self.window()
        return bool(w.windowState() & (Qt.WindowMaximized | Qt.WindowFullScreen))

    def paintEvent(self, _e: Any) -> None:
        from .ui_kit import skin
        p = QPainter(self)
        skin.paint_frame(p, QRectF(self.rect()), self.theme, self.theme.app_background(),
                         square=not self.own or self._full())
        p.end()

    # -- resizing from the edges (no system frame to do it)
    def _edges(self, pos: Any) -> Qt.Edges:
        if not self.own or self._full():
            return Qt.Edges()
        e = Qt.Edges()
        x, y, w, h = pos.x(), pos.y(), self.width(), self.height()
        if x < EDGE:
            e |= Qt.LeftEdge
        if x > w - EDGE:
            e |= Qt.RightEdge
        if y < EDGE:
            e |= Qt.TopEdge
        if y > h - EDGE:
            e |= Qt.BottomEdge
        return e

    def mouseMoveEvent(self, e: Any) -> None:
        edges = self._edges(e.position())
        cursors = {Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor, Qt.TopEdge: Qt.SizeVerCursor,
                   Qt.BottomEdge: Qt.SizeVerCursor}
        if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges in cursors:
            self.setCursor(cursors[edges])
        else:
            self.unsetCursor()
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e: Any) -> None:
        edges = self._edges(e.position())
        handle = self.window().windowHandle()
        if e.button() == Qt.LeftButton and edges and handle is not None:
            handle.startSystemResize(edges)
            return
        if e.button() == Qt.LeftButton and handle is not None and self.own:   # empty margins move it
            handle.startSystemMove()
            return
        super().mousePressEvent(e)

    def changeEvent(self, e: Any) -> None:
        super().changeEvent(e)
        self.update()


class WinButton(QAbstractButton):
    """Minimise, maximise, close: in the theme's look."""

    def __init__(self, role: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.theme = Theme()
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(30 if self._kind() != "type" else 34, 22)
        self.setToolTip({"min": "Minimise", "max": "Maximise", "close": "Close"}[role])

    def _kind(self) -> str:
        from .ui_kit import skin
        return skin.kind(self.theme)

    def enterEvent(self, e: Any) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e: Any) -> None:
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e: Any) -> None:
        t = self.theme
        k = self._kind()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, k != "step")
        hover = self.underMouse()
        r = QRectF(self.rect())
        close = self.role == "close"
        hot = QColor("#e0483e") if close and k in ("fade", "spring", "glide") else t.pop()
        if k == "type":                               # [_] [^] [x]
            p.setFont(t.mono_font())
            p.setPen(t.pop() if hover else t.muted())
            p.drawText(r, Qt.AlignCenter, {"min": "[_]", "max": "[^]", "close": "[x]"}[self.role])
            p.end()
            return
        if k in ("fade", "spring"):                   # dots that show their sign on hover
            d = 12.0 if k == "fade" else 13.0
            c = r.center()
            col = {"min": QColor("#f5c04a"), "max": QColor("#6ddc8f"), "close": QColor("#ff6b57")}[self.role] \
                if k == "spring" else (hot if hover else t.highlight())
            if k == "fade" and hover and not close:
                col = t.highlight().lighter(140)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(c, d / 2, d / 2)
            if not hover:
                p.end()
                return
            fg = QColor(0, 0, 0, 170) if k == "spring" else contrast_text(col)
            p.setPen(QPen(fg, 1.4))
            s = d * 0.22
            self._glyph(p, c, s)
            p.end()
            return
        if k == "slam":
            if hover:
                p.fillRect(r, t.pop())
            p.setPen(QPen(t.text(), 2.5))
            self._glyph(p, r.center(), 5)
            p.end()
            return
        if k == "step":
            if hover:
                p.fillRect(r.adjusted(2, 2, -2, -2), t.accent() if not close else t.pop())
            p.setPen(QPen(t.text() if not hover else t.page_background(), 2))
            self._glyph(p, r.center(), 4)
            p.end()
            return
        # Ledger, Blueprint: thin glyphs, a hairline box on hover
        if hover:
            p.setPen(QPen(t.pop() if k == "glide" else t.text(), 1))
            p.drawRect(r.adjusted(4.5, 1.5, -4.5, -1.5))
        p.setPen(QPen(t.pop() if k == "glide" else t.text(), 1.1))
        self._glyph(p, r.center(), 4.5)
        p.end()

    def _glyph(self, p: QPainter, c: QPointF, s: float) -> None:
        if self.role == "min":
            p.drawLine(QPointF(c.x() - s, c.y()), QPointF(c.x() + s, c.y()))
        elif self.role == "max":
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(c.x() - s, c.y() - s, 2 * s, 2 * s))
        else:
            p.drawLine(QPointF(c.x() - s, c.y() - s), QPointF(c.x() + s, c.y() + s))
            p.drawLine(QPointF(c.x() + s, c.y() - s), QPointF(c.x() - s, c.y() + s))


class TitleBar(QWidget):
    """A slim strip across the top: drag it to move the window, double-click to maximise; the
    window buttons sit at its right."""

    def __init__(self, window: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.win = window
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addStretch(1)
        for role in ("min", "max", "close"):
            b = WinButton(role)
            b.clicked.connect(lambda _c=False, r=role: self._act(r))
            lay.addWidget(b)
        self.setFixedHeight(24)

    def _act(self, role: str) -> None:
        if role == "min":
            self.win.showMinimized()
        elif role == "max":
            self.win.showNormal() if self.win.isMaximized() else self.win.showMaximized()
        else:
            self.win.close()

    def mousePressEvent(self, e: Any) -> None:
        handle = self.win.windowHandle()
        if e.button() == Qt.LeftButton and handle is not None:
            handle.startSystemMove()

    def mouseDoubleClickEvent(self, e: Any) -> None:
        if e.button() == Qt.LeftButton:
            self._act("max")
