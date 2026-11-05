"""
Replaces the native KDE-styled QLineEdit box with the same rounded,
accent-outlined, card-background-filled look the Library's search bar
already has -- per a direct request to apply that same box style
to every text field, not just the search bar. Text editing itself
(cursor, selection, IME, etc.) is still QLineEdit's own native
behavior; only the background/border painting is replaced, and a
stylesheet strips QLineEdit's own opaque background so this widget's
own paintEvent (drawn first) shows through underneath the native text
rendering, same layering trick CustomButton and _InfoBox already use.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLineEdit

from .theme_config import get_settings
from .theme import Theme


class CustomLineEdit(QLineEdit):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        from .skin import field_text_color
        text_color = field_text_color(self._theme).name()
        accent = self._theme.accent().name()
        # Scoped to QLineEdit specifically (not a bare "background-color:
        # ..." with no type selector) -- deliberately avoiding the exact
        # unscoped-stylesheet-cascade mistake documented in HANDOFF.md.
        self.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; color: {text_color}; "
            f"padding: 2px 8px; selection-background-color: {accent}; }}"
        )

    def paintEvent(self, event) -> None:
        from .skin import paint_field
        painter = QPainter(self)
        paint_field(painter, QRectF(self.rect()), self._theme, self.hasFocus(), self.underMouse())
        painter.end()
        super().paintEvent(event)

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)
