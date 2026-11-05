"""
Replaces native/KDE QSpinBox/QDoubleSpinBox chrome with the same
rounded, theme-colored box CustomLineEdit already gives plain text
fields -- plus two small custom-painted increment/decrement arrow
buttons (no icon asset needed; a triangle each is simple enough to
draw directly), replacing the native up/down spin arrows.

QAbstractSpinBox already does essentially everything else needed
(keyboard up/down arrows, wheel-to-step, typing a value directly,
range clamping, step size) -- only the box's own background/border
painting and the increment/decrement buttons' appearance are replaced.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QRectF
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox, QSpinBox, QAbstractButton

from .press_pulse import PressPulse
from .theme_config import get_settings
from .rounded_rect import rounded_rect_path
from .theme import Theme, contrast_text

_ARROW_WIDTH = 20


class _SpinArrowButton(QAbstractButton):
    def __init__(self, up: bool, parent=None):
        super().__init__(parent)
        self._pulse = PressPulse(self)  # shared hover/press pulse, see press_pulse.py
        self._up = up
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRepeat(True)
        self.setAutoRepeatDelay(400)
        self.setAutoRepeatInterval(80)
        appearance = get_settings()
        self._theme = Theme(appearance)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._pulse.apply(painter)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        from . import skin
        k = skin.kind(self._theme)
        bg = {"fade": self._theme.highlight(), "spring": self._theme.pop(), "slam": self._theme.pop(),
              "step": self._theme.accent()}.get(k)
        if bg is not None:
            if self.isDown():
                bg = bg.darker(125)
            elif self.underMouse():
                bg = bg.lighter(115)
            radius = {"fade": 5.0, "spring": rect.width() / 2}.get(k, 0.0)
            radius = min(radius, rect.height() / 2, rect.width() / 2)
            if k == "spring":
                bg.setAlphaF(0.55 if self.underMouse() or self.isDown() else 0.3)
            if radius:
                painter.fillPath(rounded_rect_path(rect, radius), bg)
            else:
                painter.fillRect(rect, bg)
            fg = contrast_text(bg) if k != "spring" else self._theme.text()
        else:                                    # Ledger, Console, Blueprint: just the arrow
            fg = self._theme.pop() if (self.underMouse() or self.isDown()) else self._theme.text()
        painter.setBrush(fg)
        painter.setPen(Qt.NoPen)
        cx, cy = rect.center().x(), rect.center().y()
        w, h = rect.width() * 0.3, rect.height() * 0.22
        path = QPainterPath()
        if self._up:
            path.moveTo(cx - w, cy + h * 0.5)
            path.lineTo(cx + w, cy + h * 0.5)
            path.lineTo(cx, cy - h * 0.5)
        else:
            path.moveTo(cx - w, cy - h * 0.5)
            path.lineTo(cx + w, cy - h * 0.5)
            path.lineTo(cx, cy + h * 0.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class _CustomSpinBoxMixin:
    """Shared painting + increment-button wiring for both the int and
    float variants below -- QAbstractSpinBox's own stepBy(steps) is
    what both QSpinBox and QDoubleSpinBox already implement to apply
    one step in either direction respecting range/wrapping, so the
    arrow buttons just call that directly rather than needing separate
    logic per variant."""

    def _setup_custom_chrome(self) -> None:
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)  # hide the native up/down arrows entirely
        from .skin import field_text_color
        text_color = field_text_color(self._theme).name()
        self.setStyleSheet(
            f"QAbstractSpinBox {{ background: transparent; border: none; color: {text_color}; "
            f"padding: 2px 26px 2px 8px; }}"
        )
        # The number itself is drawn by an inner QLineEdit, which the rule above doesn't reach: on a
        # desktop whose style/color scheme paints line edits itself (KDE Breeze), it kept its own
        # background and text color, or was squeezed to nothing -- the number vanished. Style it and
        # place it directly (resizeEvent) so it always shows.
        edit = self.lineEdit()
        edit.setFrame(False)
        edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; margin: 0; padding: 0; "
            f"color: {text_color}; selection-background-color: {self._theme.accent().name()}; "
            f"selection-color: {contrast_text(self._theme.accent()).name()}; }}"
        )
        self.setMinimumHeight(self.fontMetrics().height() + 10)

        self._up_btn = _SpinArrowButton(up=True, parent=self)
        self._down_btn = _SpinArrowButton(up=False, parent=self)
        self._up_btn.clicked.connect(lambda: self.stepBy(1))
        self._down_btn.clicked.connect(lambda: self.stepBy(-1))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Each arrow gets HALF the spinbox's own height, computed
        # dynamically -- NOT a fixed 22px each. A fixed 22px assumed a
        # tall spinbox (needing ~50px+ total to stack two 22px arrows
        # without overlapping), but a real spinbox is typically only
        # ~28-32px tall -- reported directly: "the bottom increment
        # button [was] the only visible one, the top increment button
        # only filling about 20% of the box," exactly what two
        # oversized, overlapping buttons anchored from opposite edges
        # would look like (the later-painted one visually winning the
        # shared space). Sizing each to exactly half the box's own
        # height guarantees they always tile perfectly with zero
        # overlap and zero gap, regardless of how tall or short this
        # particular spinbox actually is.
        margin = 2
        arrow_w = _ARROW_WIDTH
        x = self.width() - arrow_w - margin
        usable_h = self.height() - 2 * margin
        arrow_h = usable_h // 2
        self._up_btn.setGeometry(x, margin, arrow_w, arrow_h)
        self._down_btn.setGeometry(x, margin + arrow_h, arrow_w, usable_h - arrow_h)
        self._place_edit()

    def _place_edit(self) -> None:
        """The number: left of the arrows, the box's full height (see _setup_custom_chrome)."""
        margin = 2
        x = self.width() - _ARROW_WIDTH - margin
        self.lineEdit().setGeometry(8, margin, max(10, x - 8 - 4), self.height() - 2 * margin)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.FontChange):   # Qt puts it back where its style says
            self._place_edit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._place_edit()

    def enterEvent(self, event) -> None:          # the hover look (ui_kit/skin.py)
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        from .skin import paint_field
        painter = QPainter(self)
        paint_field(painter, QRectF(self.rect()), self._theme, self.hasFocus(), self.underMouse())
        painter.end()
        # Deliberately calling QAbstractSpinBox's OWN paintEvent (not
        # skipping it) so the embedded line-edit text/cursor still
        # renders normally on top of the background just painted above
        # -- same layering trick CustomLineEdit uses, just via an
        # explicit base-class call here since QSpinBox/QDoubleSpinBox
        # would otherwise repaint native chrome UNDER our own text if
        # we called their paintEvent instead of the shared base's.
        QAbstractSpinBox.paintEvent(self, event)


class CustomSpinBox(_CustomSpinBoxMixin, QSpinBox):
    def __init__(self, parent=None):
        QSpinBox.__init__(self, parent)
        self._setup_custom_chrome()


class CustomDoubleSpinBox(_CustomSpinBoxMixin, QDoubleSpinBox):
    def __init__(self, parent=None):
        QDoubleSpinBox.__init__(self, parent)
        self._setup_custom_chrome()
