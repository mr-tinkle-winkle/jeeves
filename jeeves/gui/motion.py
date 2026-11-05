"""How things move, per theme (ThemeSettings.motion):

  fade    the original: pages fade in
  glide   (Ledger)    pages fade in rising a little; the tab underline glides; slow and smooth
  spring  (Alive)     pages spring up with a small overshoot; the tab pill bounces into place
  type    (Console)   pages are revealed top to bottom like a screen redraw; the tab jumps and blinks
  slam    (Brutalist) pages slam in from the side, fast, no fade; the tab block snaps across
  step    (Arcade)    pages appear in four hard steps; everything moves in steps, like a sprite
  draw    (Blueprint) pages are drawn in left to right behind a pen line; the tab outline draws itself

Every animation here is short and runs on the GUI thread's timers only while it plays.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QParallelAnimationGroup, QPoint, QPropertyAnimation,
                            QRectF, Qt, QVariantAnimation)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget, QWidget

from .ui_kit import Theme

# duration (ms), easing
TIMING: dict[str, tuple[int, QEasingCurve.Type]] = {
    "fade": (200, QEasingCurve.OutCubic),
    "glide": (360, QEasingCurve.OutCubic),
    "spring": (480, QEasingCurve.OutBack),
    "type": (240, QEasingCurve.Linear),
    "slam": (130, QEasingCurve.OutQuad),
    "step": (320, QEasingCurve.Linear),
    "draw": (480, QEasingCurve.InOutCubic),
}


def timing(motion: str) -> tuple[int, QEasingCurve]:
    ms, kind = TIMING.get(motion, TIMING["fade"])
    curve = QEasingCurve(kind)
    if kind == QEasingCurve.OutBack:
        curve.setOvershoot(1.5)
    return ms, curve


def stepped(t: float, steps: int = 4) -> float:
    """Arcade: progress in hard steps."""
    return min(1.0, int(t * steps + 1e-6) / steps) if t < 1.0 else 1.0


def _stop(w: QWidget) -> None:
    old = getattr(w, "_fade_anim", None)
    if old is not None:
        try:
            old.finished.disconnect()
            old.stop()
        except (RuntimeError, TypeError):
            pass
        w._fade_anim = None
    cover = getattr(w, "_wipe", None)
    if cover is not None:
        try:
            cover.deleteLater()
        except RuntimeError:
            pass
        w._wipe = None
    if w.graphicsEffect() is not None:
        w.setGraphicsEffect(None)


def _ancestor_playing(w: QWidget) -> bool:
    p = w.parentWidget()
    while p is not None:
        a = getattr(p, "_fade_anim", None)
        if a is not None:
            try:
                if a.state() == QAbstractAnimation.Running:
                    return True
            except RuntimeError:
                pass
        p = p.parentWidget()
    return False


def switch(stack: QStackedWidget, index: int, motion: str | None = None) -> None:
    """Show page `index` of the stack the theme's way (immediately; the motion plays over it)."""
    if stack.currentIndex() == index:
        return
    stack.setCurrentIndex(index)
    w = stack.currentWidget()
    if w is not None:
        enter(w, motion)


def enter(w: QWidget, motion: str | None = None) -> None:
    """Play the theme's entrance on a widget that's already showing its new content. Stored as
    w._fade_anim, which Page.after_fade waits for."""
    motion = motion or Theme().motion
    _stop(w)
    if not w.isVisible() or w.width() <= 0 or _ancestor_playing(w):
        return          # (an effect inside another one's effect breaks Qt's painting; the outer one shows it)
    for child in w.findChildren(QWidget):
        if getattr(child, "_fade_anim", None) is not None:
            _stop(child)
    ms, curve = timing(motion)
    home = QPoint(w.pos())
    group = QParallelAnimationGroup(w)

    def opacity(start: float = 0.0, length: int = ms, steps: int = 0) -> None:
        effect = QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(effect)
        if steps:
            a = QVariantAnimation(w)
            a.valueChanged.connect(lambda v: effect.setOpacity(stepped(float(v), steps)))
        else:
            a = QPropertyAnimation(effect, b"opacity", w)
        a.setDuration(length)
        a.setStartValue(start)
        a.setEndValue(1.0)
        group.addAnimation(a)

    def move(dx: int, dy: int, easing: QEasingCurve) -> None:
        a = QPropertyAnimation(w, b"pos", w)
        a.setDuration(ms)
        a.setStartValue(home + QPoint(dx, dy))
        a.setEndValue(home)
        a.setEasingCurve(easing)
        group.addAnimation(a)

    if motion == "glide":
        opacity()
        move(0, 18, curve)
    elif motion == "spring":
        opacity(length=ms // 2)
        move(0, 34, curve)
    elif motion == "slam":
        move(70, 0, curve)
    elif motion == "step":
        opacity(steps=4)
    elif motion in ("type", "draw"):
        cover = _Wipe(w, motion)
        w._wipe = cover
        a = QVariantAnimation(w)
        a.setDuration(ms)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setEasingCurve(curve)
        a.valueChanged.connect(cover.set_progress)
        group.addAnimation(a)
    else:
        opacity()

    def done() -> None:
        if w.graphicsEffect() is not None:
            w.setGraphicsEffect(None)
        cov = getattr(w, "_wipe", None)
        if cov is not None:
            cov.deleteLater()
            w._wipe = None
        if motion in ("glide", "spring", "slam"):
            w.move(home)
        if getattr(w, "_fade_anim", None) is group:
            w._fade_anim = None
    group.finished.connect(done)
    w._fade_anim = group
    group.start()


class _Wipe(QWidget):
    """Covers the not-yet-revealed part of a page: Console redraws top to bottom in scan bands,
    Blueprint draws left to right behind a pen line."""

    def __init__(self, target: QWidget, motion: str) -> None:
        super().__init__(target)
        self.motion = motion
        self.p = 0.0
        self.theme = Theme()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setGeometry(target.rect())
        self.raise_()
        self.show()

    def set_progress(self, v: Any) -> None:
        self.p = float(v)
        self.update()

    def paintEvent(self, _e: Any) -> None:
        p = QPainter(self)
        bg = self.theme.page_background()
        w, h = self.width(), self.height()
        if self.motion == "type":
            bands = 8
            shown = self.p * bands
            band_h = h / bands
            for i in range(bands):
                if i + 1 <= shown:
                    continue
                y = i * band_h
                p.fillRect(QRectF(0, y, w, band_h + 1), bg)
            edge = min(h, shown * band_h)
            if self.p < 1.0:
                p.fillRect(QRectF(0, edge, w, 3), self.theme.pop())
        else:
            x = w * self.p
            p.fillRect(QRectF(x, 0, w - x, h), bg)
            if self.p < 1.0:
                pen = QPen(self.theme.pop(), 1.5)
                p.setPen(pen)
                p.drawLine(int(x), 0, int(x), h)
                dash = QPen(QColor(self.theme.pop()), 1, Qt.DashLine)
                p.setPen(dash)
                p.drawLine(int(x) + 6, 0, int(x) + 6, h)
        p.end()


def animate_value(owner: QWidget, start: Any, end: Any, motion: str, apply: Callable[[Any], None],
                  ms: int | None = None, steps: int = 0) -> QVariantAnimation:
    """A one-off value animation the theme's way (tab indicators, names, counters)."""
    length, curve = timing(motion)
    a = QVariantAnimation(owner)
    a.setDuration(ms or length)
    a.setStartValue(start)
    a.setEndValue(end)
    if motion == "step" or steps:
        n = steps or 4
        a.setEasingCurve(QEasingCurve.Linear)

        def stepped_apply(v: Any) -> None:
            t = a.currentTime() / max(1, a.duration())
            k = stepped(t, n)
            if isinstance(start, QRectF):
                apply(QRectF(start.x() + (end.x() - start.x()) * k, start.y() + (end.y() - start.y()) * k,
                             start.width() + (end.width() - start.width()) * k,
                             start.height() + (end.height() - start.height()) * k))
            elif isinstance(start, (int, float)):
                apply(start + (end - start) * k)
            else:
                apply(end if k >= 1 else start)
        a.valueChanged.connect(stepped_apply)
    else:
        a.setEasingCurve(curve)
        a.valueChanged.connect(apply)
    a.start(QAbstractAnimation.DeleteWhenStopped)
    return a
