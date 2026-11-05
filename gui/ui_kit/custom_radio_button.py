"""
Replaces native QRadioButton rendering (used for the Sort By tab's
exclusive sort-order choice) with the same card-background-box +
checkmark-icon look CustomCheckBox already gives checkboxes, just with
a circular indicator instead of a rounded-rect one, matching the
conventional round vs. square distinction between radio buttons and
checkboxes. Exclusivity itself is unchanged -- still enforced by
whatever QButtonGroup the caller adds these to, same as the native
QRadioButtons before.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QAbstractButton

from .press_pulse import PressPulse
from .theme_config import get_settings
from .resources import resource_qpixmap
from .theme import Theme

_BOX_SIZE = 20
_TEXT_GAP = 8


class CustomRadioButton(QAbstractButton):
    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._pulse = PressPulse(self)  # indicator-only pulse, see paintEvent
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        self._checkmark = resource_qpixmap("checkmark_icon.png")

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        width = _BOX_SIZE + 8 + (_TEXT_GAP + text_w if self.text() else 0)
        height = max(_BOX_SIZE, fm.height()) + 4
        return QSize(width, height)

    def paintEvent(self, event) -> None:
        """The theme's own radio (ui_kit/skin.py); the label beside it."""
        from . import skin
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        box_rect = QRectF(0, (self.height() - _BOX_SIZE) / 2, _BOX_SIZE, _BOX_SIZE)
        if skin.kind(self._theme) == "type":
            from PySide6.QtGui import QFontMetricsF
            fm = QFontMetricsF(self._theme.mono_font())
            box_rect = QRectF(0, (self.height() - fm.height()) / 2, fm.horizontalAdvance("(*)"), fm.height())
        painter.save()
        self._pulse.apply(painter, box_rect.center())       # only the indicator pulses, not the label
        skin.paint_radio(painter, box_rect, self._theme, self.isChecked(), self.underMouse())
        painter.restore()
        if self.text():
            painter.setPen(QColor(self._appearance.color_text))
            painter.drawText(self.rect().adjusted(int(box_rect.width()) + _TEXT_GAP, 0, 0, 0),
                             Qt.AlignVCenter | Qt.AlignLeft, self.text())
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)
