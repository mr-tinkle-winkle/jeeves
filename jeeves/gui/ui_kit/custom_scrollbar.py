"""
Replaces the native KDE-styled vertical scroll bar in the Library grid.
Per direct request: twice the default width, the handle in the
info/button blue (Theme.accent()), and the track (the space it scrolls
in) in the video-card-background color (Theme.card_background()).

Uses QStyleOptionSlider + the current style's own subControlRect() to
find where the handle actually is (accounting for scroll position and
handle size) rather than computing that geometry by hand -- the exact
standard Qt technique for a custom-painted scroll bar that still
behaves like a real one (draggable, clickable track, wheel-scrollable)
underneath.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QScrollBar, QStyleOptionSlider, QStyle, QApplication

from .theme_config import get_settings
from .theme import Theme


class CustomScrollBar(QScrollBar):
    def __init__(self, orientation=Qt.Vertical, parent=None):
        super().__init__(orientation, parent)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        default_extent = QApplication.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        self._extent = default_extent * 2
        if orientation == Qt.Vertical:
            self.setFixedWidth(self._extent)
        else:
            self.setFixedHeight(self._extent)

    # The scroll area lays its bar out by the bar's size hint: the style's default width, half of the
    # fixed width above -- so the bar hung out of the box and its right half was cut off.
    def sizeHint(self):
        hint = super().sizeHint()
        if self.orientation() == Qt.Vertical:
            hint.setWidth(self._extent)
        else:
            hint.setHeight(self._extent)
        return hint

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        from .skin import paint_scrollbar
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        handle_rect = self.style().subControlRect(QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarSlider, self)
        handle = QRectF(handle_rect) if handle_rect.isValid() and handle_rect.width() > 0 and \
            handle_rect.height() > 0 else None
        paint_scrollbar(painter, QRectF(self.rect()), handle, self._theme, self.isSliderDown())
        painter.end()
        # Deliberately NOT calling super().paintEvent() -- this fully
        # replaces the native scroll bar's painting; QScrollBar's own
        # mouse handling (drag, page-click, wheel) is untouched since
        # none of that lives in paintEvent.
