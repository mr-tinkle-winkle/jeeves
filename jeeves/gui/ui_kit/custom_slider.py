"""
A horizontal slider in the kit's look: a rounded track in the surface color,
filled up to the value in the accent color, and a round handle -- instead of
the native groove and handle. Dragging, clicking and the wheel are QSlider's
own; a click jumps straight to where you clicked (seek bars, volume).
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSlider, QStyle

from .theme import Theme
from .theme_config import get_settings


class CustomSlider(QSlider):
    def __init__(self, orientation=Qt.Horizontal, parent=None, colors: tuple[str, str] | None = None):
        super().__init__(orientation, parent)
        self._theme = Theme(get_settings())
        self._colors = colors                  # (track, fill) when it lives on a fixed dark surface
        self.setMinimumHeight(18)
        self.setCursor(Qt.PointingHandCursor)

    def _track(self) -> QRectF:
        h = 6.0
        return QRectF(9, (self.height() - h) / 2, max(1.0, self.width() - 18), h)

    def handle_x(self) -> float:
        t = self._track()
        span = max(1, self.maximum() - self.minimum())
        return t.left() + t.width() * (self.value() - self.minimum()) / span

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            t = self._track()
            v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x() - t.left()),
                                               int(t.width()))
            self.setValue(v)
            self.sliderMoved.emit(v)
        super().mousePressEvent(e)

    def paint_extra(self, p: QPainter, track: QRectF) -> None:
        """Subclasses draw marks on the track (chapters)."""

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._track()
        track = QColor(self._colors[0]) if self._colors else self._theme.surface()
        fill = QColor(self._colors[1]) if self._colors else self._theme.accent()
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(t, 3, 3)
        x = self.handle_x()
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(t.left(), t.top(), x - t.left(), t.height()), 3, 3)
        self.paint_extra(p, t)
        r = 7.0 if (self.isSliderDown() or self.underMouse()) else 6.0
        p.setBrush(QColor("#ffffff") if self._colors else self._theme.text())
        p.drawEllipse(QRectF(x - r, self.height() / 2 - r, 2 * r, 2 * r))
        p.end()

    def enterEvent(self, e) -> None:
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self.update()
        super().leaveEvent(e)
