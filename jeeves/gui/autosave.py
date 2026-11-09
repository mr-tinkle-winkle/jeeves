"""Saving as you go (Settings > General: "Save changes automatically", on by default).

A page with a Save button gets an AutoSave: a moment after you stop changing something on it (typing,
picking, ticking, sliding), what's on the page is saved -- quietly, without a "Saved." box. Only your
own edits count: fields being filled in from the settings (opening an agent, the model list arriving)
never trigger a save, so it can't write back a half-loaded form. Changing to another agent, leaving the
page or closing the window saves what's pending first.
"""
from __future__ import annotations

import json
import weakref
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QComboBox, QLineEdit,
                               QPlainTextEdit, QTextEdit, QWidget)

ENABLED = True            # settings: gui.autosave (the app keeps this up to date)
DELAY_MS = 900


def set_enabled(on: bool) -> None:
    global ENABLED
    ENABLED = bool(on)


class AutoSave(QObject):
    def __init__(self, root: QWidget, state: Callable[[], Any], save: Callable[[], None],
                 ready: Callable[[], bool] = lambda: True, delay_ms: int = DELAY_MS) -> None:
        super().__init__(root)
        self.root, self.state, self.save_fn, self.ready = root, state, save, ready
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay_ms)
        self.timer.timeout.connect(self.flush)
        self._watched: "weakref.WeakSet[QWidget]" = weakref.WeakSet()
        self._baseline: str | None = None
        self.on_saved: Callable[[], None] | None = None
        self.watch()

    # ------------------------------------------------------------------ what counts as an edit
    def watch(self, root: QWidget | None = None) -> None:
        """Hook up every input on the page (call again after building more of it)."""
        for w in (root or self.root).findChildren(QWidget):
            if w in self._watched:
                continue
            hooked = True
            if isinstance(w, QLineEdit):
                w.textEdited.connect(self._edited)                      # (not setText)
            elif isinstance(w, QComboBox):
                w.activated.connect(self._edited)                       # (not setCurrentIndex)
                if w.isEditable() and w.lineEdit() is not None:
                    self._watched.add(w.lineEdit())
                    w.lineEdit().textEdited.connect(self._edited)
            elif isinstance(w, QAbstractButton):
                if not w.isCheckable():
                    continue                                            # (plain buttons do their own thing)
                w.clicked.connect(self._edited)                         # (not setChecked)
            elif isinstance(w, QAbstractSpinBox):
                sig = getattr(w, "valueChanged", None)
                if sig is not None:
                    sig.connect(lambda *_a, w=w: self._edited_if_focus(w))
            elif isinstance(w, QAbstractSlider):
                w.valueChanged.connect(lambda *_a, w=w: self._edited_if(w.isSliderDown() or w.hasFocus()))
            elif isinstance(w, (QPlainTextEdit, QTextEdit)):
                if w.isReadOnly():
                    continue
                w.textChanged.connect(lambda w=w: self._edited_if_focus(w))
            else:
                hooked = False
            if hooked:
                self._watched.add(w)

    def _edited_if_focus(self, w: QWidget) -> None:
        self._edited_if(w.hasFocus() or (w.focusProxy() is not None and w.focusProxy().hasFocus()))

    def _edited_if(self, yes: bool) -> None:
        if yes:
            self._edited()

    def _edited(self, *_a: Any) -> None:
        if ENABLED:
            self.timer.start()

    # ------------------------------------------------------------------ saving
    def loaded(self) -> None:
        """What's on the page now is what's saved (call after filling it in)."""
        self.timer.stop()
        try:
            self._baseline = json.dumps(self.state(), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            self._baseline = None

    def pending(self) -> bool:
        return self.timer.isActive()

    def flush(self) -> None:
        """Saves now if something changed (a no-op otherwise)."""
        self.timer.stop()
        if not ENABLED or not self.ready():
            return
        try:
            now = json.dumps(self.state(), sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return
        if now == self._baseline:
            return
        self._baseline = now
        self.save_fn()
        if self.on_saved is not None:
            self.on_saved()
