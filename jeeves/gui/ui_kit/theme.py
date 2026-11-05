"""
Theme: the only way kit widgets get a color.

Every custom-painted fill/stroke goes through a Theme accessor rather
than reading QApplication.palette() or a hardcoded QColor at the call
site. That keeps two things possible:
- flipping app_theme_enabled swaps every widget between the app's
  fixed palette and the live system (KDE) palette, with no per-widget
  code;
- later visual passes (e.g. subtle gradients instead of flat fills)
  only have to touch this file.

Cheap to construct. Widgets build one in __init__ from get_settings().
Settings changes take effect for widgets constructed afterward (and
for any widget that re-reads settings in paintEvent). There is no
live-restyle broadcast: the app should rebuild or reopen affected
pages after saving, or tell the user to restart.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from .theme_config import ThemeSettings, get_settings


def contrast_text(bg: QColor) -> QColor:
    """Near-black or near-white, whichever reads better on `bg`
    (perceptual luminance). Use this for text/icons drawn on a themed
    fill so it stays readable whatever palette the user picks."""
    luminance = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
    return QColor(20, 20, 20) if luminance > 140 else QColor(240, 240, 240)


_WIDE_FACES = {"Press Start 2P": 0.6, "Archivo Black": 0.9}


class Theme:
    def __init__(self, settings: "ThemeSettings | None" = None):
        self._s = settings or get_settings()

    # ---- switches / shape ------------------------------------------------
    @property
    def settings(self) -> ThemeSettings:
        return self._s

    @property
    def custom_widgets_enabled(self) -> bool:
        return self._s.custom_widgets_enabled

    @property
    def app_theme_enabled(self) -> bool:
        return self._s.app_theme_enabled

    def corner_radius(self, fallback: float = 0) -> float:
        """The configured radius, or `fallback` when rounding is off.
        (Some widgets pass a small fallback, e.g. 8, so they're never
        fully square even with rounding disabled. That's a per-widget
        design choice carried over from afterglow.)"""
        return self._s.rounded_corner_radius if self._s.rounded_corners_enabled else fallback

    @property
    def padding(self) -> int:
        return self._s.ui_padding

    # ---- color roles -----------------------------------------------------
    def _pick(self, hex_value: str, palette_role) -> QColor:
        if self._s.app_theme_enabled:
            return QColor(hex_value)
        return QApplication.palette().color(palette_role)

    def accent(self) -> QColor:
        return self._pick(self._s.color_accent, QPalette.Highlight)

    def surface(self) -> QColor:
        return self._pick(self._s.color_surface, QPalette.AlternateBase)

    def app_background(self) -> QColor:
        return self._pick(self._s.color_app_background, QPalette.Window)

    def page_background(self) -> QColor:
        return self._pick(self._s.color_page_background, QPalette.Base)

    def highlight(self) -> QColor:
        return self._pick(self._s.color_highlight, QPalette.Mid)

    def text(self) -> QColor:
        return self._pick(self._s.color_text, QPalette.WindowText)

    def text_outline(self) -> QColor:
        # No sensible system-palette equivalent; always the configured value.
        return QColor(self._s.color_text_outline)

    def pop(self) -> QColor:
        return QColor(self._s.color_pop) if self._s.app_theme_enabled else self.accent()

    def muted(self) -> QColor:
        if self._s.app_theme_enabled:
            return QColor(self._s.color_muted)
        return QApplication.palette().color(QPalette.PlaceholderText)

    def shadow(self) -> "tuple[int, QColor] | None":
        """A hard offset shadow behind boxes (Arcade, Brutalist): (offset px, color), or None."""
        off = int(self._s.shadow_offset or 0)
        return (off, QColor(self._s.color_shadow)) if off > 0 else None

    @property
    def border_width(self) -> float:
        return float(self._s.border_width or 2.0)

    def body_font(self, size: float = 0, weight: int = 0) -> "QFont":
        f = QApplication.font()
        if self._s.font_body:
            f.setFamily(self._s.font_body)
        if size:
            f.setPointSizeF(size)
        if weight:
            f.setWeight(QFont.Weight(weight))
        return f

    def display_font(self, size: float = 0, weight: int = 700) -> "QFont":
        """Headings and names. Pixel and display faces read big, so callers give the size they want
        and the theme's own face is used at it."""
        f = self.body_font(size)
        if self._s.font_display:
            f.setFamily(self._s.font_display)
            if self._s.font_display in _WIDE_FACES:       # a pixel face is far wider at the same size
                f.setPointSizeF(max(6.0, f.pointSizeF() * _WIDE_FACES[self._s.font_display]))
        f.setWeight(QFont.Weight(weight))
        return f

    def mono_font(self, size: float = 0) -> "QFont":
        from PySide6.QtGui import QFontDatabase
        f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        if self._s.font_mono:
            f.setFamily(self._s.font_mono)
        f.setPointSizeF(size or QApplication.font().pointSizeF())
        return f

    def heading(self, text: str) -> str:
        return text.upper() if self._s.display_upper else text

    @property
    def motion(self) -> str:
        return self._s.motion or "fade"

    # ---- derived ---------------------------------------------------------
    def button_color(self) -> QColor:
        """Button fill. Same value as accent() today, kept as its own
        accessor because a button fill and an outline accent are
        different roles that may diverge."""
        return self.accent()

    def button_text_color(self) -> QColor:
        return contrast_text(self.button_color())
