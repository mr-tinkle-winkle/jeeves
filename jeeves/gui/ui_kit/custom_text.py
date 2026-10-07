"""
Multi-line text, rich text and list boxes in the kit's look -- the same
rounded, surface-filled, accent-outlined box CustomLineEdit has, with the
kit's own scroll bar -- instead of the native KDE-styled frames.

Like CustomLineEdit, editing/selection/scrolling stay Qt's own; only the
frame is painted here (behind a transparent viewport).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QListWidget, QPlainTextEdit, QTextBrowser

from .custom_scrollbar import CustomScrollBar
from .rounded_rect import rounded_rect_path
from .theme import Theme, contrast_text
from .theme_config import get_settings


def _paint_box(widget, theme: Theme, appearance) -> None:
    painter = QPainter(widget)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(widget.rect()).adjusted(1, 1, -1, -1)
    radius = appearance.rounded_corner_radius if appearance.rounded_corners_enabled else 8
    radius = min(radius, 14) if radius else 0
    path = rounded_rect_path(rect, radius) if radius else None
    if path is not None:
        painter.fillPath(path, theme.surface())
    else:
        painter.fillRect(rect, theme.surface())
    pen = painter.pen()
    pen.setColor(theme.accent())
    pen.setWidthF(2)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if path is not None:
        painter.drawPath(path)
    else:
        painter.drawRect(rect)
    painter.end()


class _BoxMixin:
    def _setup_box(self, selector: str) -> None:
        self._appearance = get_settings()
        self._theme = Theme(self._appearance)
        text = contrast_text(self._theme.surface()).name()
        accent = self._theme.accent().name()
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            f"{selector} {{ background: transparent; border: none; color: {text}; padding: 6px; "
            f"selection-background-color: {accent}; selection-color: {contrast_text(self._theme.accent()).name()}; }}")
        self.viewport().setAutoFillBackground(False)
        self.setVerticalScrollBar(CustomScrollBar(Qt.Vertical))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def paintEvent(self, event) -> None:          # the frame first, under the (transparent) viewport
        _paint_box(self.viewport(), self._theme, self._appearance)
        super().paintEvent(event)


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
