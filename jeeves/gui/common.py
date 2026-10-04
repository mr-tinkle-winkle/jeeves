"""Shared Qt plumbing for the GUI and the overlay: theme provider, daemon
bridge (requests + pushed events as Qt signals), small helpers."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, fields
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QWidget

from .. import ipc, paths
from .ui_kit import Theme, ThemeSettings, set_settings_provider

# Jeeves' own colors. None were provided yet, so these are the kit's
# placeholder monochrome scheme (UI_THEMING_GUIDE.md, section 2).
JEEVES_THEME_DEFAULTS = ThemeSettings()


class _ThemeCache:
    """Reads the theme straight from settings.json (cached by mtime) so the
    kit's provider stays cheap and works before the daemon answers."""

    def __init__(self) -> None:
        self._key = None
        self._theme = ThemeSettings()

    def get(self) -> ThemeSettings:
        files = [paths.settings_file(), paths.system_settings_file()]
        key = []
        for f in files:
            try:
                st = f.stat()
                key.append((st.st_mtime_ns, st.st_size))
            except OSError:
                key.append(None)
        key_t = tuple(key)
        if key_t != self._key:
            merged: dict[str, Any] = asdict(JEEVES_THEME_DEFAULTS)
            for f in files:
                try:
                    data = json.loads(f.read_text()).get("theme") or {}
                except (OSError, ValueError):
                    data = {}
                merged.update({k: v for k, v in data.items() if k in {fl.name for fl in fields(ThemeSettings)}})
            self._theme = ThemeSettings(**merged)
            self._key = key_t
        return self._theme


_theme_cache = _ThemeCache()


def install_theme() -> None:
    set_settings_provider(_theme_cache.get)


def theme_palette(widget: QWidget) -> None:
    """Background + text roles through QPalette (never an unscoped stylesheet)."""
    theme = Theme()
    widget.setAutoFillBackground(True)
    pal = widget.palette()
    pal.setColor(widget.backgroundRole(), theme.app_background())
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(role, theme.text())
    pal.setColor(QPalette.Base, theme.surface())
    widget.setPalette(pal)


class Daemon(QObject):
    """Request/response on a worker thread + pushed events as signals."""

    event = Signal(str, object)
    connected = Signal(bool)
    _result = Signal(object, object, object)   # callback, result, error

    def __init__(self, topics: list[str] | None = None) -> None:
        super().__init__()
        self.client = ipc.Client(timeout=15)
        self._lock = threading.Lock()
        self._result.connect(self._deliver)
        self.online = False
        self.sub = ipc.Subscriber(topics or ["*"], self._on_event, self._on_disconnect)
        self.sub.start()
        self._probe = QTimer(self)
        self._probe.setInterval(2000)
        self._probe.timeout.connect(self._check)
        self._probe.start()
        QTimer.singleShot(0, self._check)

    def _on_event(self, topic: str, data: Any) -> None:
        if not self.online:
            self.online = True
            self.connected.emit(True)
        self.event.emit(topic, data)

    def _on_disconnect(self) -> None:
        if self.online:
            self.online = False
            self.connected.emit(False)

    def _check(self) -> None:
        def go() -> None:
            try:
                with self._lock:
                    self.client.call("ping", timeout=2)
                ok = True
            except ipc.DaemonError:
                ok = False
            if ok != self.online:
                self.online = ok
                self.connected.emit(ok)
        threading.Thread(target=go, daemon=True).start()

    def call(self, method: str, callback: Callable[[Any], None] | None = None,
             error: Callable[[str], None] | None = None, **params: Any) -> None:
        """Asynchronous: callback(result) / error(message) run on the GUI thread."""
        def go() -> None:
            try:
                with self._lock:
                    res = self.client.call(method, **params)
                self._result.emit(callback, res, None)
            except ipc.DaemonError as exc:
                self._result.emit(error, None, str(exc))
        threading.Thread(target=go, daemon=True).start()

    def call_sync(self, method: str, **params: Any) -> Any:
        with self._lock:
            return self.client.call(method, **params)

    def _deliver(self, fn: Any, result: Any, err: Any) -> None:
        if fn is None:
            if err:
                from .ui_kit import show_message
                show_message(None, "Jeeves", err)
            return
        fn(err if err is not None else result)

    def stop(self) -> None:
        self.sub.stop()
        self.client.close()


def flags_overlay() -> Qt.WindowType:
    return (Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.X11BypassWindowManagerHint
            | Qt.WindowDoesNotAcceptFocus)
