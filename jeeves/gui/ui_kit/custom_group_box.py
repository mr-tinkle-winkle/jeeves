"""
Replaces native/KDE QGroupBox chrome (used for every "section" header
in Settings, plus tag-category grouping in the Sort popover's Filters
page) with a custom-painted rounded box and title, matching the rest
of the Afterglow custom-widget system.

Deliberately a drop-in for the exact construction pattern already used
everywhere: `group = CustomGroupBox("Title")` then
`layout = QVBoxLayout(group)` (or QFormLayout, etc.) works completely
unchanged at every call site -- this box applies its own reserved
margins (room at the top for the painted title) to whatever layout
gets attached, both via a setLayout() override AND, as a second,
independent guarantee, again in showEvent() right before it's ever
actually shown (see showEvent's own comment for why the setLayout()
path alone isn't fully reliable). `widget.setContentsMargins()` called
BEFORE a layout exists does NOT carry over to a layout attached
later -- confirmed directly (a widget's contentsMargins set pre-layout
has no effect; a newly-attached layout falls back to the current
style's own default margins instead). That wrong assumption in an
earlier version of this class is exactly what caused a real reported
bug: every CustomGroupBox was actually laying out its content with
the STYLE's default margins the whole time, not the title-reserving
ones this class was written to use -- meaning the reserved space above
the title never actually existed, misaligning what this box's OWN
paintEvent assumed versus what its content actually needed, and
throwing off any code (like the Sort popover's own page-sizing) that
relied on this box's reported sizeHint being consistent with its real
rendered content.
"""
from __future__ import annotations

from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget, QVBoxLayout

from .theme_config import get_settings
from .theme import Theme

_TITLE_HEIGHT = 26


class CustomGroupBox(QWidget):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        appearance = get_settings()
        self._appearance = appearance
        self._theme = Theme(appearance)

    def make_layout(self, layout_cls=QVBoxLayout):
        """Constructs a layout of the given class, attaches it to this
        box, and immediately applies the correct title-reserving
        margins -- use THIS instead of calling `layout_cls(self)`
        directly. PySide6 does not reliably invoke a Python-side
        setLayout() override from that constructor's own internal
        attachment call (confirmed directly -- a subclass's setLayout()
        override, with a print statement added specifically to check,
        never actually printed when a layout was attached this way),
        which is what an earlier version of this class relied on to
        apply these margins automatically. showEvent() below is a
        second, independent attempt at the same fix for anything that
        still constructs a layout the old way, but it only helps once
        this box is actually SHOWN -- code that queries this box's
        sizeHint() before ever showing it (the Sort popover's own page-
        sizing, which computes a page's needed height immediately after
        building it, well before any show() call) would still see the
        wrong, un-reserved size at exactly the moment that matters most.
        This factory method is the actually-reliable fix; showEvent()
        stays as a defensive fallback, not the primary mechanism."""
        layout = layout_cls(self)
        self._apply_layout_margins()
        return layout

    def setLayout(self, layout) -> None:
        super().setLayout(layout)
        self._apply_layout_margins()

    def showEvent(self, event) -> None:
        # BELT AND SUSPENDERS: setLayout() above is the "should" fix,
        # but calling QVBoxLayout(self)/QFormLayout(self) etc. --
        # every call site's own construction pattern -- turned out NOT
        # to reliably invoke a Python-subclass override of setLayout()
        # at all (confirmed directly: the margins it tried to apply
        # simply weren't there afterward), almost certainly because
        # that constructor attaches the layout via an internal C++-side
        # call that PySide's virtual-method dispatch doesn't route
        # through the Python override for. Re-applying here as well,
        # right before this box is ever actually shown, is a second,
        # independent guarantee that doesn't depend on that dispatch
        # working -- cheap and fully idempotent either way.
        super().showEvent(event)
        self._apply_layout_margins()

    def _title_h(self) -> int:
        from PySide6.QtGui import QFontMetricsF
        from . import skin
        k = skin.kind(self._theme)
        if k == "glide":
            f = self._theme.display_font(15, 600)
            return int(QFontMetricsF(f).height() + 2)
        if k == "draw":
            return int(QFontMetricsF(self._theme.display_font(12, 600)).height() + 2)
        return _TITLE_HEIGHT

    def _apply_layout_margins(self) -> None:
        from . import skin
        lay = self.layout()
        if lay is not None:
            lay.setContentsMargins(*skin.group_margins(self._theme, self._title_h()))

    def _number(self) -> int:
        """Its place among the section boxes next to it (Blueprint numbers them)."""
        par = self.parentWidget()
        if par is None:
            return 0
        boxes = [w for w in par.findChildren(CustomGroupBox) if w.parentWidget() is par]
        boxes.sort(key=lambda w: (w.y(), w.x()))
        return boxes.index(self) + 1 if self in boxes else 0

    def setTitle(self, title: str) -> None:
        self._title = title
        self.update()

    def title(self) -> str:
        return self._title

    def paintEvent(self, event) -> None:
        from . import skin
        painter = QPainter(self)
        skin.paint_group(painter, self, self._theme.heading(self._title) if self._title else "", self._theme,
                         self._title_h(), self._number())
        painter.end()
