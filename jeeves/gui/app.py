"""The Jeeves settings window: a front-end only. Every change is a request to
the daemon; the window works on whatever the daemon reports back."""
from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from .common import Daemon, install_theme, theme_palette


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("jeeves")
    app.setDesktopFileName("jeeves")
    from ..resources import app_icon
    app.setWindowIcon(app_icon())
    install_theme()            # before any kit widget is built
    w = MainWindow()
    w.show()
    code = app.exec()
    w.daemon.stop()
    return code


class MainWindow(QMainWindow):
    _power_done = Signal(str)          # from the thread that starts/stops the daemon

    def __init__(self) -> None:
        super().__init__()
        from .pages_agents import AgentsPage
        from .pages_functions import FunctionsPage
        from .pages_misc import (AccountsPage, AppearancePage, DryRunPage, GeneralPage, HistoryPage,
                                 IndicatorsPage, TrainingPage, WikipediaPage)
        from .pages_models import ModelsPage
        from .ui_kit import CustomCheckBox, SegmentButton, Theme, compute_scale, crossfade_to_index, show_message

        self._show_message = show_message
        self._compute_scale = compute_scale
        self.setWindowTitle("Jeeves")
        self.resize(1280, 820)
        self.daemon = Daemon()
        self.settings: dict[str, Any] = {}
        self.locked: set[str] = set()
        theme = Theme()

        central = QWidget()
        self.setCentralWidget(central)
        theme_palette(central)
        rowl = QHBoxLayout(central)
        pad = theme.padding
        rowl.setContentsMargins(pad, pad, pad, pad)
        rowl.setSpacing(pad)

        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(max(4, pad // 2))
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.stack = QStackedWidget()

        send = self.send
        self.pages = [
            ("Agents", AgentsPage(self.daemon)),
            ("Functions", FunctionsPage(self.daemon, send)),
            ("Models", ModelsPage(self.daemon, send)),
            ("Listening", GeneralPage(self.daemon, send)),
            ("Indicators", IndicatorsPage(self.daemon, send)),
            ("Accounts", AccountsPage(self.daemon, send)),
            ("History", HistoryPage(self.daemon)),
            ("Dry Run", DryRunPage(self.daemon)),
            ("Training", TrainingPage(self.daemon, send)),
            ("Wikipedia", WikipediaPage(self.daemon, send)),
            ("Appearance", AppearancePage(self.daemon, send)),
        ]
        for i, (title, page) in enumerate(self.pages):
            btn = SegmentButton(text=title, position="full")
            btn.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
            side.addWidget(btn, 1)
            self.nav.addButton(btn, i)
            self.stack.addWidget(page)
        # the master switch: off = every AI stops and unloads (same as `jeeves --toggle`)
        self.power = CustomCheckBox("Jeeves on")
        self.power.setToolTip("Off stops the Jeeves daemon (every AI model, listening, keybinds); on starts a "
                              "fresh one. Same as `jeeves --toggle`.")
        self.power.toggled.connect(self._power_toggled)
        self._power_done.connect(self._after_power)
        side.addWidget(self.power)
        self.status = QLabel("Connecting to the daemon…")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        side.addWidget(self.status)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(lambda i: (crossfade_to_index(self.stack, i), self._refresh_page(i)))

        rowl.addWidget(self.sidebar)
        rowl.addWidget(self.stack, 1)

        self.daemon.connected.connect(self._connected)
        self.daemon.event.connect(self._event)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)
        self._reload_timer.timeout.connect(self.reload)

    # ------------------------------------------------------------------
    def send(self, changes: dict[str, Any]) -> None:
        def failed(err: str) -> None:
            self._show_message(self, "Couldn't save", err)
            self.reload()
        self.daemon.call("settings.set", None, failed, changes=changes)

    def reload(self) -> None:
        def got(res: Any) -> None:
            if not isinstance(res, dict):
                return
            self.settings = res.get("value", {})
            self.locked = set(res.get("locked", []))
            self.power.blockSignals(True)
            self.power.setChecked(bool(self.settings.get("general", {}).get("enabled", True)))
            self.power.setEnabled("general.enabled" not in self.locked)
            self.power.blockSignals(False)
            self._refresh_page(self.stack.currentIndex())
        self.daemon.call("settings.get", got, lambda _e: None)

    def _power_toggled(self, on: bool) -> None:
        import threading

        from .. import service
        self.power.setEnabled(False)
        self.status.setText("Starting Jeeves…" if on else "Stopping Jeeves…")

        def go() -> None:
            err = ""
            try:
                service.power_on() if on else service.power_off()
            except Exception as exc:  # noqa: BLE001 -- shown to the user
                err = str(exc)
            self._power_done.emit(err)
        threading.Thread(target=go, daemon=True).start()

    def _after_power(self, err: str) -> None:
        self.power.setEnabled(True)
        if err:
            self._show_message(self, "Jeeves", err)
        self.daemon._check()           # the poll reports the new state (and reloads settings when on)
        self._connected(self.daemon.online)

    def _refresh_page(self, i: int) -> None:
        if self.settings and 0 <= i < len(self.pages):
            self.pages[i][1].refresh(self.settings, self.locked)

    def _connected(self, ok: bool) -> None:
        if ok:
            self.status.setText("Daemon running")
        else:
            from .. import config
            off = not config.Settings().get("general.enabled", True)
            self.status.setText("Jeeves is off —\nturn it on to change settings" if off else
                                "Daemon not running —\nsystemctl --user start jeeves")
            self.power.blockSignals(True)
            self.power.setChecked(False)
            self.power.blockSignals(False)
        self.stack.setEnabled(ok)
        if ok:
            self.reload()

    def _event(self, topic: str, data: Any) -> None:
        if topic == "settings":
            self._reload_timer.start()
        elif topic in ("functions", "import_done"):
            self._refresh_page(self.stack.currentIndex())
        page = self.stack.currentWidget()
        handler = getattr(page, "on_event", None)
        if handler:
            handler(topic, data)
        if topic == "history" and isinstance(page, type(self.pages[6][1])):
            page.refresh(self.settings, self.locked)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.sidebar.setFixedWidth(max(110, min(170, round(self.width() * 0.1))))
        self._compute_scale(self.width(), self.height())


if __name__ == "__main__":
    sys.exit(main())
