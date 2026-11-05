"""
Replaces the native KDE-styled QCheckBox indicator with a small custom-
painted box: video-card-background fill, accent-colored outline, and
the provided checkmark icon drawn in when checked (nothing when
not). FilterCheckBox (library_page.py) builds on this to add a third
"blocked" state (the x icon) for the Library's Filters list -- by request,
that third state is specific to Filters and shouldn't appear anywhere
else this base class is used.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import QAbstractButton

from .press_pulse import PressPulse, scaled_cached
from .theme_config import get_settings
from .rounded_rect import rounded_rect_path
from .resources import resource_qpixmap
from .theme import Theme

_BOX_SIZE = 20
_TEXT_GAP = 8


class CustomCheckBox(QAbstractButton):
    def __init__(self, text: str = "", parent=None, leading_icon=None):
        super().__init__(parent)
        self._pulse = PressPulse(self)  # indicator-only pulse, see paintEvent
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)
        self._checkmark = resource_qpixmap("checkmark_icon.png")
        # The TAG's own custom icon (if one is set), shown between the
        # checkbox indicator and its label -- distinct from _checkmark
        # (which shows INSIDE the indicator box to mark checked/
        # unchecked state) or FilterCheckBox's block-state x icon.
        # Added by direct request to show these "in the
        # dropdowns" (context menu, quick-action Filters menu, Sort
        # popover) alongside the checkbox itself, not just on the
        # video card.
        self._leading_icon = leading_icon
        # the indicator moves between off and on the theme's way (a sliding/springing knob, a cross
        # that draws itself, a ring that fills); 0..1
        self._on = 0.0
        self._on_anim = None
        self.toggled.connect(self._animate_to)

    def _skinned(self) -> bool:
        return type(self)._icon_for_state is CustomCheckBox._icon_for_state

    def _animate_to(self, checked: bool) -> None:
        from PySide6.QtCore import QEasingCurve, QVariantAnimation
        from . import skin
        target = 1.0 if checked else 0.0
        if self._on_anim is not None:
            self._on_anim.stop()
        if not self.isVisible():
            self._on = target
            self.update()
            return
        k = skin.kind(self._theme)
        ms, curve = {"spring": (420, QEasingCurve.OutBack), "glide": (260, QEasingCurve.OutCubic),
                     "draw": (320, QEasingCurve.InOutCubic), "fade": (180, QEasingCurve.OutCubic)}.get(
            k, (1, QEasingCurve.Linear))
        a = QVariantAnimation(self)
        a.setDuration(ms)
        a.setStartValue(self._on)
        a.setEndValue(target)
        ec = QEasingCurve(curve)
        if curve == QEasingCurve.OutBack:
            ec.setOvershoot(2.2)
        a.setEasingCurve(ec)
        a.valueChanged.connect(self._set_on)
        self._on_anim = a
        a.start()

    def _set_on(self, v) -> None:
        self._on = float(v)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._on_anim is None or self._on_anim.state() != self._on_anim.State.Running:
            self._on = 1.0 if self.isChecked() else 0.0

    def _box(self):
        from PySide6.QtGui import QFontMetricsF
        from . import skin
        if not self._skinned():
            return float(_BOX_SIZE), float(_BOX_SIZE)
        return skin.check_size(self._theme, QFontMetricsF(self._theme.mono_font()))

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        bw, bh = self._box()
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        icon_w = _BOX_SIZE + _TEXT_GAP if self._has_leading_icon() else 0
        width = int(bw + icon_w + (_TEXT_GAP + text_w if self.text() else 0)) + 2
        height = int(max(bh, fm.height())) + 4
        return QSize(width, height)

    def _has_leading_icon(self) -> bool:
        return self._leading_icon is not None and not self._leading_icon.isNull()

    def _icon_for_state(self):
        """Which icon (if any) fills the box right now -- overridden by
        FilterCheckBox to add its third "blocked" state on top of the
        plain checked/unchecked this base class knows about."""
        return self._checkmark if self.isChecked() else None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        bw, bh = self._box()
        if self._skinned():
            from . import skin
            if not (self._on_anim and self._on_anim.state() == self._on_anim.State.Running):
                self._on = 1.0 if self.isChecked() else 0.0
            box = QRectF(0, (self.height() - bh) / 2, bw, bh)
            painter.save()
            self._pulse.apply(painter, box.center())
            skin.paint_check(painter, box, self._theme, self._on, self.underMouse(), self.isEnabled())
            painter.restore()
            if not self.isEnabled():
                painter.setOpacity(0.45)
            if self.text():
                painter.setPen(QColor(self._appearance.color_text))
                painter.drawText(self.rect().adjusted(int(bw) + _TEXT_GAP, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft,
                                 self.text())
            painter.end()
            return
        box_rect = QRectF(0, (self.height() - _BOX_SIZE) / 2, _BOX_SIZE, _BOX_SIZE)
        # Pulse only the indicator box (restored before the label is
        # drawn) so the text doesn't slide around as the box scales.
        painter.save()
        self._pulse.apply(painter, box_rect.center())
        radius = self._appearance.rounded_corner_radius if self._appearance.rounded_corners_enabled else 0
        radius = min(radius, _BOX_SIZE / 2) if radius else 0

        bg = self._theme.surface()
        if self.underMouse():
            bg = bg.lighter(115)
        path = rounded_rect_path(box_rect, radius) if radius else None
        if path:
            painter.fillPath(path, bg)
        else:
            painter.fillRect(box_rect, bg)

        pen = painter.pen()
        pen.setColor(self._theme.accent())
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if path:
            painter.drawPath(path)
        else:
            painter.drawRect(box_rect)

        icon = self._icon_for_state()
        if icon is not None and not icon.isNull():
            margin = 3
            target = box_rect.adjusted(margin, margin, -margin, -margin)
            scaled = scaled_cached(icon, round(target.width()), round(target.height()))
            x = target.x() + (target.width() - scaled.width()) / 2
            y = target.y() + (target.height() - scaled.height()) / 2
            painter.drawPixmap(round(x), round(y), scaled)

        painter.restore()

        text_x_offset = _BOX_SIZE + _TEXT_GAP
        if self._has_leading_icon():
            leading_rect = QRectF(_BOX_SIZE + _TEXT_GAP, (self.height() - _BOX_SIZE) / 2, _BOX_SIZE, _BOX_SIZE)
            scaled_leading = scaled_cached(self._leading_icon, round(leading_rect.width()), round(leading_rect.height()))
            lx = leading_rect.x() + (leading_rect.width() - scaled_leading.width()) / 2
            ly = leading_rect.y() + (leading_rect.height() - scaled_leading.height()) / 2
            painter.drawPixmap(round(lx), round(ly), scaled_leading)
            text_x_offset += _BOX_SIZE + _TEXT_GAP

        if self.text():
            painter.setPen(QColor(self._appearance.color_text))
            text_rect = self.rect().adjusted(text_x_offset, 0, 0, 0)
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)
