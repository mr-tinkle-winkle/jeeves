"""Settings-bound widgets built from the UI kit.

Every bound widget knows its dotted settings path. Changes are sent to the
daemon right away (the GUI never writes settings itself). Paths declared in
NixOS come back as locked: the widget is disabled and shows a lock.
"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
                               QSizePolicy, QVBoxLayout, QWidget)

from .ui_kit import (CustomButton, CustomCheckBox, CustomDoubleSpinBox, CustomGroupBox, CustomLineEdit,
                     CustomSpinBox, SmoothScrollArea, Theme, combo_box_stylesheet, get_settings,
                     paint_page_outline)

LOCK = "🔒"


def get_path(tree: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def is_locked(locked: set[str], path: str) -> bool:
    return any(path == p or path.startswith(p + ".") or p.startswith(path + ".") for p in locked)


class Page(QWidget):
    """A top-level page: own background, 3 px outline, always scrollable."""

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__()
        self._theme = Theme()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        pad = self._theme.padding
        self.body_layout.setContentsMargins(pad, pad, pad, pad)
        self.body_layout.setSpacing(pad)
        heading = QLabel(title)
        heading.setStyleSheet(f"QLabel {{ color: {self._theme.text().name()}; font-size: 20px; font-weight: bold; }}")
        self.body_layout.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setWordWrap(True)
            self.body_layout.addWidget(sub)
        scroll.setWidget(self.body)
        self.body.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(scroll)

    def paintEvent(self, _e: Any) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), self._theme.page_background())
        p.end()
        paint_page_outline(self, self._theme.page_background())

    def section(self, title: str) -> QVBoxLayout:
        box = CustomGroupBox(title)
        lay = box.make_layout(QVBoxLayout)
        self.body_layout.addWidget(box)
        return lay

    def finish(self) -> None:
        self.body_layout.addStretch(1)

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        """Called with fresh settings whenever they change."""


def _hint_style() -> str:
    return f"QLabel {{ font-size: 11px; color: {Theme().text().darker(125).name()}; }}"


def discard(w: QWidget | None) -> None:
    """Remove a widget safely. setParent(None) hands it to Python's garbage collector,
    which deletes the C++ object immediately -- even mid-animation or while Qt is still
    delivering one of its events -- and crashes the GUI. deleteLater() waits until Qt is
    done with it."""
    if w is None:
        return
    w.hide()
    w.setParent(None)
    _graveyard.append(w)          # keep the Python wrapper alive until Qt deletes it
    w.destroyed.connect(lambda *_a, w=w: _graveyard.remove(w) if w in _graveyard else None)
    w.deleteLater()


_graveyard: list[QWidget] = []


def clear_layout(layout: Any) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            discard(item.widget())


def combo() -> QComboBox:
    c = QComboBox()
    c.setStyleSheet(combo_box_stylesheet(get_settings()))
    c.setFocusPolicy(Qt.StrongFocus)
    c.wheelEvent = lambda e: e.ignore()        # don't change values while scrolling the page
    return c


def label(text: str, wrap: bool = True) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    return lab


def row(*widgets: QWidget, stretch_last: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for i, x in enumerate(widgets):
        lay.addWidget(x, 1 if (stretch_last and i == len(widgets) - 1) else 0)
    if not stretch_last:
        lay.addStretch(1)
    return w


class Binder:
    """Creates bound widgets and keeps them in sync with the daemon."""

    def __init__(self, send: Callable[[dict[str, Any]], None]) -> None:
        self.send = send
        self.items: list[tuple[str, QWidget, Callable[[Any], None], QLabel | None]] = []
        self._muted = False

    def _register(self, path: str, w: QWidget, setter: Callable[[Any], None], lock_label: QLabel | None) -> None:
        self.items.append((path, w, setter, lock_label))

    def _emit(self, path: str, value: Any) -> None:
        if not self._muted:
            self.send({path: value})

    def form_row(self, layout: QVBoxLayout, text: str, w: QWidget, path: str, setter: Callable[[Any], None],
                 hint: str = "") -> QWidget:
        lab = QLabel(text)
        lab.setMinimumWidth(240)
        lock = QLabel("")
        line = QWidget()
        h = QHBoxLayout(line)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(lab)
        h.addWidget(w, 1)
        h.addWidget(lock)
        layout.addWidget(line)
        if hint:
            hl = label(hint)
            hl.setStyleSheet(_hint_style())
            hl.setContentsMargins(4, 0, 0, 4)
            layout.addWidget(hl)
        self._register(path, w, setter, lock)
        return line

    def check(self, layout: QVBoxLayout, text: str, path: str, hint: str = "") -> CustomCheckBox:
        cb = CustomCheckBox(text)
        cb.toggled.connect(lambda v: self._emit(path, bool(v)))
        lock = QLabel("")
        line = row(cb, lock)
        layout.addWidget(line)
        if hint:
            hl = label(hint)
            hl.setStyleSheet(_hint_style())
            hl.setContentsMargins(30, 0, 0, 4)
            layout.addWidget(hl)
        self._register(path, cb, lambda v: cb.setChecked(bool(v)), lock)
        return cb

    def text(self, layout: QVBoxLayout, text: str, path: str, hint: str = "", placeholder: str = "",
             empty_is_none: bool = False) -> CustomLineEdit:
        e = CustomLineEdit()
        e.setPlaceholderText(placeholder)
        e.editingFinished.connect(lambda: self._emit(path, (e.text().strip() or None) if empty_is_none else e.text()))
        self.form_row(layout, text, e, path, lambda v: e.setText("" if v is None else str(v)), hint)
        return e

    def list_text(self, layout: QVBoxLayout, text: str, path: str, hint: str = "", sep: str = ",",
                  placeholder: str = "") -> CustomLineEdit:
        e = CustomLineEdit()
        e.setPlaceholderText(placeholder)

        def out() -> None:
            vals = [x.strip() for x in e.text().split(sep) if x.strip()]
            self._emit(path, vals)
        e.editingFinished.connect(out)
        self.form_row(layout, text, e, path, lambda v: e.setText(f"{sep} ".join(v or [])), hint)
        return e

    def number(self, layout: QVBoxLayout, text: str, path: str, lo: float, hi: float, step: float = 1,
               decimals: int = 0, hint: str = "", suffix: str = "") -> QWidget:
        if decimals:
            s = CustomDoubleSpinBox()
            s.setDecimals(decimals)
        else:
            s = CustomSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        if suffix:
            s.setSuffix(suffix)
        s.wheelEvent = lambda ev: ev.ignore()
        s.editingFinished.connect(lambda: self._emit(path, s.value()))
        self.form_row(layout, text, s, path, lambda v: s.setValue(v if v is not None else lo), hint)
        return s

    def choice(self, layout: QVBoxLayout, text: str, path: str, options: list[tuple[str, Any]],
               hint: str = "") -> QComboBox:
        c = combo()
        for lab, val in options:
            c.addItem(lab, val)
        c.activated.connect(lambda i: self._emit(path, c.itemData(i)))

        def setter(v: Any) -> None:
            i = c.findData(v)
            if i < 0 and v is not None:
                c.addItem(str(v), v)
                i = c.count() - 1
            c.setCurrentIndex(max(0, i))
        self.form_row(layout, text, c, path, setter, hint)
        return c

    def color(self, layout: QVBoxLayout, text: str, path: str) -> CustomButton:
        b = CustomButton("")
        b.setMinimumWidth(120)
        state = {"value": "#000000"}

        def pick() -> None:
            got = QColorDialog.getColor(QColor(state["value"]), None, text)
            if got.isValid():
                state["value"] = got.name()
                b.set_fill_color(got)
                b.setText(got.name())
                self._emit(path, got.name())

        def setter(v: Any) -> None:
            state["value"] = v or "#000000"
            b.set_fill_color(QColor(state["value"]))
            b.setText(state["value"])
        b.clicked.connect(pick)
        self.form_row(layout, text, b, path, setter)
        return b

    def code(self, layout: QVBoxLayout, text: str, path: str, hint: str = "") -> QPlainTextEdit:
        e = QPlainTextEdit()
        e.setMinimumHeight(80)
        e.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        timer = QTimer(e)
        timer.setSingleShot(True)
        timer.setInterval(800)
        timer.timeout.connect(lambda: self._emit(path, e.toPlainText()))
        e.textChanged.connect(lambda: None if self._muted else timer.start())
        layout.addWidget(label(text))
        layout.addWidget(e)
        if hint:
            layout.addWidget(label(hint))
        self._register(path, e, lambda v: e.setPlainText(v or "") if e.toPlainText() != (v or "") else None, None)
        return e

    def load(self, settings: dict[str, Any], locked: set[str]) -> None:
        self._muted = True
        try:
            for path, w, setter, lock in self.items:
                if w.hasFocus() and not isinstance(w, (CustomCheckBox,)):
                    continue     # don't fight the user's typing
                setter(get_path(settings, path))
                lk = is_locked(locked, path)
                w.setEnabled(not lk)
                if lock is not None:
                    lock.setText(LOCK if lk else "")
                    lock.setToolTip("Declared in NixOS (services.jeeves.settings) -- change it there" if lk else "")
        finally:
            self._muted = False
