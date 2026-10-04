"""On-screen indicators and popups. Started (and restarted) by the daemon, so
everything here works without the settings GUI open.

* Indicators, in the configured corner: microphone (click = +5 s, hold = keep
  listening until release +1 s), the transcript under it, and a spinner per
  active request coloured by stage:
    thinking gray · researching blue · responding black with a white outline ·
    asking for input flashing white · unclear purple · unavailable flashing red.
  Clicking the spinner: thinking/researching -> the agent's current thoughts
  (and what it's looking at); responding -> pause/resume; asking -> proceed.
* Timers in the bottom-right corner.
* Marked screen positions (circles).
* Popups: Manual Response Review, Text Request, imported-function approval,
  typed answers, notices.

Two processes, both started by the daemon:

* ``jeeves overlay`` -- the indicators, timers, screen marks and notices. On
  Wayland they are layer-shell surfaces on the overlay layer (``layershell.py``,
  the same way afterglow's clip indicator works); without the layer-shell shim
  it falls back to XWayland override-redirect windows.
* ``jeeves overlay --popups`` -- Manual Response Review, Text Request, typed
  answers, thoughts and import approval: ordinary windows that need the
  keyboard, so they can't live in the layer-shell process (that setting turns
  every window of a process into a layer surface).
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

from .util import format_duration, graphical_env


def _single_instance(name: str) -> Any:
    """One overlay per session: a second copy exits (the first keeps working and
    reconnects to a restarted daemon by itself)."""
    import fcntl

    from . import paths
    paths.ensure(paths.runtime_dir())
    f = open(paths.runtime_dir() / f"{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def main(popups: bool = False) -> int:
    lock = _single_instance("popups" if popups else "overlay")
    if lock is None:
        return 0
    env = graphical_env()
    for k in ("WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP"):
        if k in env and k not in os.environ:
            os.environ[k] = env[k]
    from . import layershell
    use_layer = False
    if not popups:
        forced = os.environ.get("QT_QPA_PLATFORM", "")
        if os.environ.get("WAYLAND_DISPLAY") and forced in ("", "wayland") and layershell.enable_in_this_process():
            os.environ["QT_QPA_PLATFORM"] = "wayland"
            use_layer = True
        elif not forced:
            # no layer-shell: XWayland override-redirect windows are the next best thing
            os.environ["QT_QPA_PLATFORM"] = "xcb" if os.environ.get("DISPLAY") else "wayland"

    from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout,
                                   QWidget)

    from .gui.common import Daemon, flags_overlay, install_theme, theme_palette
    from .gui.ui_kit import (CustomButton, CustomCheckBox, CustomLineEdit, SmoothScrollArea, Theme,
                             paint_page_outline)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("jeeves-popups" if popups else "jeeves-overlay")
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("jeeves.overlay").info("%s on %s (layer-shell: %s)", "popups" if popups else "indicators",
                                             QGuiApplication.platformName(), use_layer)

    def target_screen() -> Any:
        """Which monitor overlays appear on (Indicators > Screen): the primary one, the one
        the mouse is on, or a named output. Layer surfaces without an output get one
        chosen by the compositor -- often not the one you're looking at."""
        choice = (settings.get("indicators") or {}).get("screen", "mouse") or "mouse"
        screens = QGuiApplication.screens()
        if choice not in ("mouse", "primary"):
            named = next((sc for sc in screens if sc.name() == choice), None)
            if named is not None:
                return named
        if choice == "mouse":
            try:
                from PySide6.QtCore import QPoint

                from .daemon import desktop as dk
                x, y = dk.mouse_position()
                found = QGuiApplication.screenAt(QPoint(x, y))
                if found is not None:
                    return found
            except Exception:  # noqa: BLE001 -- no kdotool/hyprctl: fall back to primary
                pass
        try:
            from .daemon import desktop as dk
            name = dk.primary_output()
        except Exception:  # noqa: BLE001
            name = None
        named = next((sc for sc in screens if sc.name() == name), None) if name else None
        return named or QGuiApplication.primaryScreen()

    def present(w: QWidget, edges: list[str], margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> None:
        """Show an overlay window: a layer surface anchored to edges (Wayland), or
        placed by geometry beforehand (X11). Re-anchors if the edges or screen changed.
        The screen is chosen when the window appears, never while it is showing."""
        screen = target_screen() if not w.isVisible() else getattr(w, "_screen", None)
        key = (tuple(edges), tuple(margins), screen.name() if screen is not None else "")
        if use_layer and w.isVisible() and getattr(w, "_layer_key", None) != key:
            w.hide()
        if not w.isVisible():
            w._screen = screen
            if use_layer:
                w.winId()
                win = w.windowHandle()
                if screen is not None and win is not None and win.screen() is not screen:
                    win.setScreen(screen)
                if getattr(w, "_layer_key", None) != key:
                    layershell.configure(win, edges, margins)
                    w._layer_key = key
            w.show()

    def set_input(w: QWidget, rects: list[Any]) -> None:
        """Wayland layer surface: clicks only land on these rects; elsewhere they pass
        through to the windows below (Qt maps the mask to the input region)."""
        if not use_layer:
            return
        win = w.windowHandle()
        if win is None:
            return
        from PySide6.QtGui import QRegion
        key = tuple(tuple(int(v) for v in (r.left(), r.top(), r.width(), r.height())) for r in rects)
        if key == getattr(w, "_input_key", None):
            return
        w._input_key = key
        if rects:
            region = QRegion()
            for r in rects:
                region = region.united(QRegion(r.toAlignedRect()))
            win.setFlags(win.flags() & ~Qt.WindowTransparentForInput)
            win.setMask(region)
        else:
            win.setMask(QRegion())
            win.setFlags(win.flags() | Qt.WindowTransparentForInput)

    def open_popup(kind: str, **data: Any) -> None:
        """From the indicator process: ask the popups process to open a window."""
        daemon.call("ui.popup", None, lambda _e: None, kind=kind, data=data)

    install_theme()
    daemon = Daemon()
    settings: dict[str, Any] = {}

    def load_settings(*_: Any) -> None:
        def got(res: Any) -> None:
            if isinstance(res, dict):
                settings.clear()
                settings.update(res.get("value", {}))
                indicator.relayout()
                timers_w.relayout()
        daemon.call("settings.get", got, lambda _e: None)

    # ------------------------------------------------------------------ indicator
    class Indicator(QWidget):
        MIC = 0

        def __init__(self) -> None:
            super().__init__(None, flags_overlay())
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setWindowTitle("Jeeves indicator")
            self.setMouseTracking(True)
            self.states: dict[str, dict[str, Any]] = {}
            self.transcript = ""
            self.transcript_until = 0.0
            self.response = ""
            self.response_until = 0.0
            self.phase = 0.0
            self.press_at = 0.0
            self.holding = False
            self.hits: list[tuple[QRectF, str, str]] = []   # rect, kind, request id
            self.menu: dict[str, Any] | None = None          # right-click menu: {"rid", "anchor"}
            self.menu_until = 0.0
            self.anim = QTimer(self)
            self.anim.setInterval(33)
            self.anim.timeout.connect(self._tick)
            self.anim.start()
            self.hold_timer = QTimer(self)
            self.hold_timer.setSingleShot(True)
            self.hold_timer.setInterval(350)
            self.hold_timer.timeout.connect(self._start_hold)

        def cfg(self, key: str, default: Any = None) -> Any:
            return (settings.get("indicators") or {}).get(key, default)

        def color(self, stage: str) -> QColor:
            cols = self.cfg("colors", {}) or {}
            defaults = {"thinking": "#808080", "researching": "#2f6fff", "responding": "#000000",
                        "asking": "#ffffff", "unclear": "#8a2be2", "unavailable": "#ff2020", "listening": "#ffffff",
                        "transcript": "#808080"}
            return QColor(cols.get(stage) or defaults.get(stage, "#808080"))

        def relayout(self) -> None:
            size = int(self.cfg("size", 56))
            w = max(420, size * 8)
            rows = max(1, len(self.visible_states()))
            h = size * rows + 140
            corner = self.cfg("corner", "top-right")
            pad = 16
            if use_layer:
                self.resize(int(w), int(h))
            else:
                scr = target_screen().availableGeometry()
                x = scr.x() + pad if "left" in corner else scr.x() + scr.width() - w - pad
                y = scr.y() + pad if corner.startswith("top") else scr.y() + scr.height() - h - pad
                self.setGeometry(int(x), int(y), int(w), int(h))
            if self.visible_states() or time.time() < max(self.transcript_until, self.response_until):
                present(self, layershell.corner_edges(corner), (pad, pad, pad, pad))
            else:
                self.hide()

        def visible_states(self) -> list[dict[str, Any]]:
            out = []
            for s in self.states.values():
                if s["stage"] == "listening" and not self.cfg("stt_active", True):
                    continue
                if s["stage"] not in ("listening", "transcript") and not self.cfg("processing", True):
                    continue
                out.append(s)
            return sorted(out, key=lambda s: s.get("time", 0))

        def update_state(self, st: dict[str, Any]) -> None:
            rid = st.get("request")
            if st.get("stage") == "idle":
                self.states.pop(rid, None)
            elif st.get("stage") == "transcript":
                self.states.pop(rid, None)
                if self.cfg("stt_output", True):
                    self.transcript = st.get("detail", "")
                    self.transcript_until = time.time() + 4
            else:
                self.states[rid] = st
            self.relayout()
            self.update()

        def show_response(self, text: str) -> None:
            if not self.cfg("response_text", True):
                return
            self.response = text
            self.response_until = time.time() + min(20, 4 + len(text) / 18)
            self.relayout()
            self.update()

        def _tick(self) -> None:
            self.phase = (self.phase + 0.033) % 1000
            if self.isVisible():
                if not self.visible_states() and time.time() > max(self.transcript_until, self.response_until):
                    self.hide()
                self.update()

        # -------------------------------------------------------------- painting
        def paintEvent(self, _e: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            size = int(self.cfg("size", 56))
            right = "right" in self.cfg("corner", "top-right")
            self.hits = []
            y = 6
            for st in self.visible_states():
                stage = st["stage"]
                x = self.width() - size - 6 if right else 6
                rect = QRectF(x, y, size, size)
                if stage == "listening":
                    self._mic(p, rect, st)
                    self.hits.append((rect, "mic", st["request"]))
                else:
                    self._spinner(p, rect, st)
                    self.hits.append((rect, "spinner", st["request"]))
                label = st.get("agent_name") or ("Listening" if stage == "listening" else "")
                detail = st.get("detail") or ""
                text = f"{label}: {detail}" if detail and label else (detail or label)
                self._label(p, rect, text, right)
                y += size + 8
            if time.time() < self.transcript_until and self.transcript:
                self._bubble(p, y, f"“{self.transcript}”", right, QColor(30, 30, 30, 220))
                y += 40
            if time.time() < self.response_until and self.response:
                self._bubble(p, y, self.response, right, QColor(0, 0, 0, 230))
            self._paint_menu(p, right)
            p.end()
            set_input(self, [r for r, _k, _i in self.hits])

        # -------------------------------------------------------------- right-click menu
        def _paint_menu(self, p: QPainter, right: bool) -> None:
            """Drawn inside this surface rather than as a QMenu popup window: popups
            attached to layer surfaces aren't reliable across compositors."""
            m = self.menu
            if m is None:
                return
            if m["rid"] not in self.states or time.time() > self.menu_until:
                self.menu = None
                return
            st = self.states[m["rid"]]
            items = [("suspend", "Resume" if st.get("suspended") else "Suspend"), ("close", "Close")]
            anchor: QRectF = m["anchor"]
            w, ih = 150.0, 32.0
            x = anchor.left() - w - 8 if right else anchor.right() + 8
            box = QRectF(x, anchor.top(), w, ih * len(items) + 8)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(24, 24, 24, 245))
            p.drawRoundedRect(box, 10, 10)
            f = QFont()
            f.setPointSize(11)
            p.setFont(f)
            for i, (action, text) in enumerate(items):
                r = QRectF(box.left() + 4, box.top() + 4 + i * ih, w - 8, ih)
                if self.menu.get("hover") == action:
                    p.setBrush(QColor(255, 255, 255, 40))
                    p.drawRoundedRect(r, 7, 7)
                p.setPen(QColor("#ffffff"))
                p.drawText(r.adjusted(12, 0, -8, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
                p.setPen(Qt.NoPen)
                self.hits.insert(0, (r, f"menu:{action}", m["rid"]))

        def _flash(self, period: float = 0.8) -> float:
            return 0.5 + 0.5 * math.sin(self.phase * 2 * math.pi / period)

        def _mic(self, p: QPainter, r: QRectF, st: dict[str, Any]) -> None:
            p.setPen(Qt.NoPen)
            glow = QColor(255, 255, 255, int(60 + 80 * self._flash(1.2)))
            p.setBrush(glow)
            p.drawEllipse(r)
            p.setBrush(QColor(20, 20, 20, 235))
            p.drawEllipse(r.adjusted(4, 4, -4, -4))
            c = r.center()
            s = r.width() / 56
            pen = QPen(QColor("#ffffff"))
            pen.setWidthF(3 * s)
            p.setPen(pen)
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(QRectF(c.x() - 6 * s, c.y() - 15 * s, 12 * s, 20 * s), 6 * s, 6 * s)
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(c.x() - 11 * s, c.y() - 10 * s, 22 * s, 20 * s), 200 * 16, 140 * 16)
            p.drawLine(QPointF(c.x(), c.y() + 10 * s), QPointF(c.x(), c.y() + 15 * s))
            if st.get("suspended"):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawEllipse(r.adjusted(4, 4, -4, -4))
                p.setBrush(QColor("#ffffff"))
                c2 = r.center()
                p.drawRect(QRectF(c2.x() - 7, c2.y() - 8, 5, 16))
                p.drawRect(QRectF(c2.x() + 2, c2.y() - 8, 5, 16))
            if st.get("detail") == "held" or self.holding:
                p.setPen(QPen(QColor("#ffcc00"), 3 * s))
                p.drawEllipse(r.adjusted(2, 2, -2, -2))

        def _spinner(self, p: QPainter, r: QRectF, st: dict[str, Any]) -> None:
            stage = st["stage"]
            base = self.color(stage)
            if st.get("color") and stage in ("thinking",):
                base = QColor(st["color"])
            alpha = 255
            if stage == "asking":
                alpha = int(80 + 175 * self._flash(0.9))
            if stage == "unavailable":
                alpha = int(60 + 195 * self._flash(0.4))
            base.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawEllipse(r)
            width = r.width() * 0.14
            inner = r.adjusted(width, width, -width, -width)
            if stage == "responding":
                outline = QColor((settings.get("indicators") or {}).get("colors", {}).get("responding_outline", "#ffffff"))
                pen = QPen(outline, width + 4, Qt.SolidLine, Qt.RoundCap)
                p.setPen(pen)
                p.drawArc(inner, 0, 360 * 16)
            pen = QPen(base, width, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            if stage in ("asking", "unavailable", "unclear") or st.get("paused") or st.get("suspended"):
                p.drawArc(inner, 0, 360 * 16)
            else:
                start = int((-self.phase * 360 * 1.4) % 360 * 16)
                p.drawArc(inner, start, 270 * 16)
            if st.get("paused") or st.get("suspended"):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffffff"))
                c = r.center()
                p.drawRect(QRectF(c.x() - 7, c.y() - 8, 5, 16))
                p.drawRect(QRectF(c.x() + 2, c.y() - 8, 5, 16))

        def _label(self, p: QPainter, r: QRectF, text: str, right: bool) -> None:
            if not text:
                return
            f = QFont()
            f.setPointSize(10)
            p.setFont(f)
            metrics = p.fontMetrics()
            maxw = self.width() - r.width() - 30
            shown = metrics.elidedText(text, Qt.ElideRight, int(maxw))
            tw = metrics.horizontalAdvance(shown) + 16
            box = QRectF(r.left() - tw - 8 if right else r.right() + 8, r.center().y() - 13, tw, 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 200))
            p.drawRoundedRect(box, 10, 10)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignCenter, shown)

        def _bubble(self, p: QPainter, y: int, text: str, right: bool, bg: QColor) -> None:
            f = QFont()
            f.setPointSize(11)
            p.setFont(f)
            w = self.width() - 12
            rect = p.fontMetrics().boundingRect(0, 0, w - 20, 400, Qt.TextWordWrap, text)
            box = QRectF(6, y, min(w, rect.width() + 20), min(400, rect.height() + 14))
            if right:
                box.moveRight(self.width() - 6)
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(box, 12, 12)
            p.setPen(QColor("#ffffff"))
            p.drawText(box.adjusted(10, 7, -10, -7), Qt.TextWordWrap, text)

        # -------------------------------------------------------------- input
        def _hit(self, pos: Any) -> tuple[str, str] | None:
            for rect, kind, rid in self.hits:
                if rect.contains(pos):
                    return kind, rid
            return None

        def mousePressEvent(self, e: Any) -> None:
            hit = self._hit(e.position())
            if e.button() == Qt.RightButton:
                if hit and hit[0] in ("mic", "spinner"):
                    rect = next(r for r, k, i in self.hits if i == hit[1] and k == hit[0])
                    self.menu = {"rid": hit[1], "anchor": QRectF(rect)}
                    self.menu_until = time.time() + 10
                else:
                    self.menu = None
                self.update()
                return
            if hit and hit[0] == "mic":
                self.press_at = time.time()
                self.hold_timer.start()

        def mouseMoveEvent(self, e: Any) -> None:
            if self.menu is not None:
                hit = self._hit(e.position())
                hover = hit[0][5:] if hit and hit[0].startswith("menu:") else None
                if hover != self.menu.get("hover"):
                    self.menu["hover"] = hover
                    self.update()

        def _start_hold(self) -> None:
            self.holding = True
            daemon.call("mic", None, lambda _e: None, action="hold")

        def mouseReleaseEvent(self, e: Any) -> None:
            if e.button() == Qt.RightButton:
                return
            hit = self._hit(e.position())
            self.hold_timer.stop()
            if hit and hit[0].startswith("menu:"):
                method = "request.suspend" if hit[0] == "menu:suspend" else "request.close"
                daemon.call(method, None, lambda _e: None, request=hit[1])
                self.menu = None
                self.update()
                return
            if self.menu is not None:          # any other click dismisses the menu
                self.menu = None
                self.update()
            if self.holding:
                self.holding = False
                daemon.call("mic", None, lambda _e: None, action="release")
                return
            if not hit:
                return
            kind, rid = hit
            if kind == "mic":
                daemon.call("mic", None, lambda _e: None, action="click")
                return
            st = self.states.get(rid, {})
            if st.get("waiting") == "answer":
                open_popup("answer", request=rid, question=st.get("detail", ""), choices=st.get("choices"))
                return

            def clicked(res: Any) -> None:
                if res == "thoughts":
                    open_popup("thoughts", request=rid, state=st)
            daemon.call("indicator.click", clicked, lambda _e: None, request=rid)

    # ------------------------------------------------------------------ timers
    class Timers(QWidget):
        def __init__(self) -> None:
            super().__init__(None, flags_overlay() | Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.items: list[dict[str, Any]] = []
            self.received = time.time()
            t = QTimer(self)
            t.setInterval(250)
            t.timeout.connect(self.update)
            t.start()

        def set_items(self, items: list[dict[str, Any]]) -> None:
            self.items = items or []
            self.received = time.time()
            self.relayout()

        def relayout(self) -> None:
            enabled = (settings.get("indicators") or {}).get("timers", True)
            if not self.items or not enabled:
                self.hide()
                return
            w, h = 260, 40 * len(self.items) + 8
            if use_layer:
                self.resize(w, h)
            else:
                scr = target_screen().availableGeometry()
                self.setGeometry(scr.x() + scr.width() - w - 16, scr.y() + scr.height() - h - 16, w, h)
            present(self, ["bottom", "right"], (16, 16, 16, 16))

        def paintEvent(self, _e: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            f = QFont()
            f.setPointSize(12)
            p.setFont(f)
            elapsed = time.time() - self.received
            for i, t in enumerate(self.items):
                box = QRectF(4, 4 + i * 40, self.width() - 8, 34)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 200))
                p.drawRoundedRect(box, 12, 12)
                remaining = max(0.0, float(t.get("remaining", 0)) - elapsed)
                total = max(1.0, float(t.get("total", 1)))
                frac = 1 - remaining / total
                bar = QRectF(box.left(), box.bottom() - 4, box.width() * frac, 4)
                p.setBrush(QColor("#2f6fff"))
                p.drawRoundedRect(bar, 2, 2)
                p.setPen(QColor("#ffffff"))
                label = t.get("label") or ("schedule" if t.get("kind") == "schedule" else "timer")
                p.drawText(box.adjusted(12, 0, -12, -2), Qt.AlignVCenter | Qt.AlignLeft, label)
                p.drawText(box.adjusted(12, 0, -12, -2), Qt.AlignVCenter | Qt.AlignRight, format_duration(remaining))
            p.end()

    # ------------------------------------------------------------------ screen marks
    class Marks(QWidget):
        def __init__(self) -> None:
            super().__init__(None, flags_overlay() | Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.marks: list[dict[str, Any]] = []
            t = QTimer(self)
            t.setInterval(33)
            t.timeout.connect(self._tick)
            t.start()

        def add(self, m: dict[str, Any]) -> None:
            scr = target_screen()
            geo = scr.geometry() if use_layer else scr.virtualGeometry()
            self.setGeometry(geo)
            m = dict(m, until=time.time() + float(m.get("seconds", 4)), born=time.time())
            self.marks.append(m)
            present(self, ["top", "bottom", "left", "right"])

        def _tick(self) -> None:
            now = time.time()
            self.marks = [m for m in self.marks if m["until"] > now]
            if not self.marks:
                self.hide()
            else:
                self.update()

        def paintEvent(self, _e: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            geo = (getattr(self, "_screen", None) or QGuiApplication.primaryScreen()).geometry() if use_layer \
                else self.geometry()
            for m in self.marks:
                age = time.time() - m["born"]
                r = float(m.get("radius", 40)) * (1 + 0.08 * math.sin(age * 6))
                c = QPointF(m["x"] - geo.x(), m["y"] - geo.y())
                p.setPen(QPen(QColor(0, 0, 0, 160), 7))
                p.drawEllipse(c, r, r)
                p.setPen(QPen(QColor("#ff3b30"), 4))
                p.drawEllipse(c, r, r)
            p.end()

    # ------------------------------------------------------------------ themed popups
    class Popup(QDialog):
        def __init__(self, title: str) -> None:
            super().__init__(None, Qt.Dialog | Qt.WindowStaysOnTopHint)
            self.setWindowTitle(title)
            theme_palette(self)
            self.theme = Theme()
            self.lay = QVBoxLayout(self)
            pad = self.theme.padding
            self.lay.setContentsMargins(pad, pad, pad, pad)
            self.lay.setSpacing(pad)

        def paintEvent(self, _e: Any) -> None:
            p = QPainter(self)
            p.fillRect(self.rect(), self.theme.page_background())
            p.end()
            paint_page_outline(self, self.theme.page_background())

        def heading(self, text: str) -> QLabel:
            lab = QLabel(text)
            lab.setStyleSheet(f"QLabel {{ font-size: 16px; font-weight: bold; color: {self.theme.text().name()}; }}")
            lab.setWordWrap(True)
            self.lay.addWidget(lab)
            return lab

    class Thoughts(Popup):
        current: "Thoughts | None" = None

        @classmethod
        def open(cls, rid: str, st: dict[str, Any]) -> None:
            if cls.current is not None:
                cls.current.close()
            cls.current = cls(rid, st)
            cls.current.show()

        def __init__(self, rid: str, st: dict[str, Any]) -> None:
            super().__init__(f"{st.get('agent_name', 'Agent')} — thoughts")
            self.rid = rid
            self.title = self.heading(f"{st.get('agent_name', 'Agent')} is {st.get('stage', 'thinking')}")
            self.looking = QLabel("")
            self.looking.setWordWrap(True)
            self.lay.addWidget(self.looking)
            self.text = QPlainTextEdit()
            self.text.setReadOnly(True)
            self.lay.addWidget(self.text, 1)
            self.resize(560, 420)
            daemon.call("request.thoughts", self._fill, lambda _e: None, request=rid)

        def _fill(self, res: Any) -> None:
            if not isinstance(res, dict):
                return
            self.text.setPlainText("".join(t if t.endswith("\n") else t + "\n" for t in res.get("thoughts", [])))
            if res.get("looking_at"):
                self.looking.setText(f"Looking at: {res['looking_at']}")

        def on_thought(self, data: dict[str, Any]) -> None:
            if data.get("request") != self.rid:
                return
            cur = self.text.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(data.get("text", "") if data.get("append") else "\n" + data.get("text", ""))
            self.text.setTextCursor(cur)
            if data.get("looking_at"):
                self.looking.setText(f"Looking at: {data['looking_at']}")

    class AnswerBox(Popup):
        def __init__(self, rid: str | None, question: str, choices: list[str] | None = None) -> None:
            super().__init__("Answer")
            self.rid = rid
            self.heading(question or "Your answer")
            row = QHBoxLayout()
            if choices:
                for c in choices:
                    b = CustomButton(str(c))
                    b.clicked.connect(lambda _=False, c=c: self._send(str(c)))
                    row.addWidget(b)
                self.lay.addLayout(row)
            self.edit = CustomLineEdit()
            self.edit.returnPressed.connect(lambda: self._send(self.edit.text()))
            self.lay.addWidget(self.edit)
            send = CustomButton("Send")
            send.clicked.connect(lambda: self._send(self.edit.text()))
            self.lay.addWidget(send, alignment=Qt.AlignRight)
            self.resize(440, 160)

        def _send(self, text: str) -> None:
            if text.strip():
                daemon.call("request.answer", None, lambda _e: None, request=self.rid, text=text.strip())
            self.close()

    class TextRequest(Popup):
        current: "TextRequest | None" = None

        def __init__(self) -> None:
            super().__init__("Jeeves — Text Request")
            self.heading("Type a request (start with the agent's name)")
            self.edit = CustomLineEdit()
            self.edit.setPlaceholderText("Jeeves, set a timer for 10 minutes")
            self.edit.returnPressed.connect(self._send)
            self.lay.addWidget(self.edit)
            self.resize(560, 120)

        def showEvent(self, e: Any) -> None:
            super().showEvent(e)
            self.activateWindow()
            self.edit.setFocus()

        def _send(self) -> None:
            text = self.edit.text().strip()
            if text:
                daemon.call("request.text", None, None, text=text)
            self.close()

    class Review(Popup):
        current: "Review | None" = None

        def __init__(self) -> None:
            super().__init__("Jeeves — Manual Response Review")
            self.items: list[dict[str, Any]] = []
            self.index = 0
            nav = QHBoxLayout()
            self.prev = CustomButton("‹")
            self.next = CustomButton("›")
            self.prev.clicked.connect(lambda: self.step(1))
            self.next.clicked.connect(lambda: self.step(-1))
            self.pos = QLabel("")
            nav.addWidget(self.prev)
            nav.addWidget(self.pos, 1, Qt.AlignCenter)
            nav.addWidget(self.next)
            self.lay.addLayout(nav)
            self.title = self.heading("")
            self.body = QPlainTextEdit()
            self.body.setReadOnly(True)
            self.lay.addWidget(self.body, 1)
            self.resize(720, 560)
            self.reload()

        def reload(self) -> None:
            def got(res: Any) -> None:
                if isinstance(res, list):
                    self.items = res
                    self.index = 0
                    self.render()
            daemon.call("history.list", got, lambda _e: None, limit=100)

        def step(self, d: int) -> None:
            if self.items:
                self.index = max(0, min(len(self.items) - 1, self.index + d))
                self.render()

        def keyPressEvent(self, e: Any) -> None:
            if e.key() == Qt.Key_Left:
                self.step(1)
            elif e.key() == Qt.Key_Right:
                self.step(-1)
            else:
                super().keyPressEvent(e)

        def render(self) -> None:
            if not self.items:
                self.title.setText("No responses yet")
                self.body.setPlainText("")
                self.pos.setText("")
                return
            e = self.items[self.index]
            self.pos.setText(f"{self.index + 1} of {len(self.items)}  ·  {time.strftime('%a %H:%M:%S', time.localtime(e['time']))}")
            self.title.setText(f"{e.get('agent')}: “{e.get('text')}”")
            lines = [f"Response: {e.get('response') or '—'}", f"Status: {e.get('status')}",
                     f"Function: {e.get('function')} {json.dumps(e.get('args'))}", "", "Everything it did:"]
            for t in e.get("trace", []):
                rest = {k: v for k, v in t.items() if k not in ("t", "kind")}
                lines.append(f"  {t.get('t', 0):6.2f}s  {t.get('kind'):<14} {json.dumps(rest, default=str)}")
            self.body.setPlainText("\n".join(lines))
            self.prev.setEnabled(self.index < len(self.items) - 1)
            self.next.setEnabled(self.index > 0)

    class ImportApproval(Popup):
        def __init__(self, req: dict[str, Any]) -> None:
            super().__init__("Jeeves — Add functions?")
            self.req = req
            self.heading(f"“{req['app']}” wants to add {len(req['items'])} function(s) to Jeeves")
            scroll = SmoothScrollArea()
            scroll.setWidgetResizable(True)
            inner = QWidget()
            col = QVBoxLayout(inner)
            self.boxes: dict[str, Any] = {}
            for item in req["items"]:
                f = item["function"]
                cb = CustomCheckBox(f"{f['name']} — {f.get('description', '')}")
                cb.setChecked(not item["problems"])
                cb.setEnabled(not item["problems"])
                self.boxes[f["name"]] = cb
                col.addWidget(cb)
                uses = ", ".join(item.get("default_functions_used") or []) or "none"
                info = QLabel(f"Uses default functions: {uses}" +
                              (f"\nReplaces the existing version from {req['app']}" if item.get("replaces") else "") +
                              ("\nProblems: " + "; ".join(item["problems"]) if item["problems"] else ""))
                info.setWordWrap(True)
                info.setContentsMargins(28, 0, 0, 8)
                col.addWidget(info)
            col.addStretch(1)
            scroll.setWidget(inner)
            inner.setAutoFillBackground(False)
            scroll.viewport().setAutoFillBackground(False)
            self.lay.addWidget(scroll, 1)
            row = QHBoxLayout()
            deny = CustomButton("Deny all")
            deny.clicked.connect(self._deny)
            ok = CustomButton("Add selected")
            ok.clicked.connect(self._approve)
            row.addStretch(1)
            row.addWidget(deny)
            row.addWidget(ok)
            self.lay.addLayout(row)
            self.resize(640, 460)

        def _approve(self) -> None:
            names = [n for n, cb in self.boxes.items() if cb.isChecked()]
            daemon.call("functions.decide", None, None, id=self.req["id"], approve=names)
            self.close()

        def _deny(self) -> None:
            daemon.call("functions.decide", None, None, id=self.req["id"], deny_all=True)
            self.close()

    class Notice(QWidget):
        def __init__(self, text: str) -> None:
            super().__init__(None, flags_overlay() | Qt.WindowTransparentForInput)
            self.setAttribute(Qt.WA_TranslucentBackground)
            self.text = text
            if use_layer:
                self.resize(520, 60)
            else:
                scr = target_screen().availableGeometry()
                self.setGeometry(scr.x() + (scr.width() - 520) // 2, scr.y() + 40, 520, 60)
            QTimer.singleShot(6000, self.close)
            present(self, ["top"], (40, 0, 0, 0))

        def paintEvent(self, _e: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 220))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 14, 14)
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect().adjusted(14, 0, -14, 0), Qt.AlignCenter | Qt.TextWordWrap, self.text)
            p.end()

    if popups:
        indicator = timers_w = marks = None
    else:
        indicator = Indicator()
        timers_w = Timers()
        marks = Marks()
    keep: list[Any] = []

    def popup(w: Any) -> None:
        keep.append(w)
        w.finished.connect(lambda *_: keep.remove(w) if w in keep else None)
        w.show()
        w.raise_()
        w.activateWindow()

    def on_event(topic: str, data: Any) -> None:
        if not popups:
            if topic == "indicator":
                indicator.update_state(data)
            elif topic == "response":
                indicator.show_response(data.get("text", ""))
            elif topic == "timers":
                timers_w.set_items(data)
            elif topic == "mark":
                marks.add(data)
            elif topic == "notice":
                keep.append(Notice(data.get("text", "")))
            elif topic == "settings":
                load_settings()
            return
        if topic == "popup":
            d = data.get("data") or {}
            if data.get("kind") == "answer":
                popup(AnswerBox(d.get("request"), d.get("question", ""), d.get("choices")))
            elif data.get("kind") == "thoughts":
                Thoughts.open(d.get("request"), d.get("state") or {})
        elif topic == "thoughts" and Thoughts.current is not None and Thoughts.current.isVisible():
            Thoughts.current.on_thought(data)
        elif topic == "show_review":
            if Review.current is not None and Review.current.isVisible():
                Review.current.reload()
            else:
                Review.current = Review()
                popup(Review.current)
        elif topic == "show_text_request":
            TextRequest.current = TextRequest()
            popup(TextRequest.current)
        elif topic == "import_request":
            popup(ImportApproval(data))
        elif topic == "history" and Review.current is not None and Review.current.isVisible():
            Review.current.reload()

    gone_since = [0.0]

    def watchdog() -> None:
        # the daemon went away for good (not just a restart): exit
        if daemon.online:
            gone_since[0] = 0.0
        elif not gone_since[0]:
            gone_since[0] = time.time()
        elif time.time() - gone_since[0] > 30:
            app.quit()

    dog = QTimer()
    dog.setInterval(2000)
    dog.timeout.connect(watchdog)
    dog.start()

    def on_connected(ok: bool) -> None:
        if not ok:
            return
        if popups:
            daemon.call("functions.pending", lambda reqs: [popup(ImportApproval(r)) for r in (reqs or [])],
                        lambda _e: None)
        else:
            load_settings()
            daemon.call("indicator.state", lambda states: [indicator.update_state(s) for s in (states or [])],
                        lambda _e: None)
            daemon.call("timers.list", timers_w.set_items, lambda _e: None)

    daemon.event.connect(on_event)
    daemon.connected.connect(on_connected)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(popups="--popups" in sys.argv))
