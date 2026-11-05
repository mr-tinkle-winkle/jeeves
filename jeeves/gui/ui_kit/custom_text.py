"""
Multi-line text, rich text and list boxes in the kit's look -- the same
rounded, surface-filled, accent-outlined box CustomLineEdit has, with the
kit's own scroll bar -- instead of the native KDE-styled frames.

Like CustomLineEdit, editing/selection/scrolling stay Qt's own; only the
frame is painted here (behind a transparent viewport).
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QListWidget, QPlainTextEdit, QTextBrowser

from .custom_scrollbar import CustomScrollBar
from .theme import Theme, contrast_text
from .theme_config import get_settings


def _paint_box(widget, theme: Theme, appearance) -> None:
    from .skin import paint_field
    painter = QPainter(widget)
    focus = widget.hasFocus() and not getattr(widget, "isReadOnly", lambda: False)()
    paint_field(painter, QRectF(widget.rect()), theme, focus, False, multiline=True)
    painter.end()


class _BoxMixin:
    """The box is painted on the whole widget, and the text and the scroll bar sit inside its border --
    painted on the text area alone, the scroll bar hung outside the box."""

    def _setup_box(self, selector: str) -> None:
        self._appearance = get_settings()
        self._theme = Theme(self._appearance)
        from .skin import field_text_color
        text = field_text_color(self._theme).name()
        accent = self._theme.accent().name()
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"{selector} {{ background: transparent; border: none; color: {text}; padding: 4px; "
            f"selection-background-color: {accent}; selection-color: {contrast_text(self._theme.accent()).name()}; }}")
        self.viewport().setAutoFillBackground(False)
        self.setVerticalScrollBar(CustomScrollBar(Qt.Vertical))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        radius = self._appearance.rounded_corner_radius if self._appearance.rounded_corners_enabled else 8
        inset = 3 + (min(radius, 14) // 4 if radius else 0)      # clear of the border and its round corners
        self.setContentsMargins(inset, inset, inset, inset)

    def event(self, event) -> bool:
        if event.type() == QEvent.Paint:          # the box itself; the text and scroll bar paint on top
            _paint_box(self, self._theme, self._appearance)
            return True
        if event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.update()
        return super().event(event)


class CustomPlainTextEdit(_BoxMixin, QPlainTextEdit):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._setup_box("QPlainTextEdit")


class CustomTextBrowser(_BoxMixin, QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_box("QTextBrowser")


class CustomListWidget(_BoxMixin, QListWidget):
    """Rows highlight in the accent color; the selected row reads in a contrasting color."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_box("QListWidget")
        accent = self._theme.accent()
        hover = self._theme.highlight()
        self.setStyleSheet(self.styleSheet() + (
            f" QListWidget::item {{ padding: 4px 6px; border-radius: 6px; }}"
            f" QListWidget::item:hover {{ background: {hover.name()}; }}"
            f" QListWidget::item:selected {{ background: {accent.name()}; color: {contrast_text(accent).name()}; }}"))
