"""The Jeeves settings window: a front-end only. Every change is a request to
the daemon; the window works on whatever the daemon reports back."""
from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QStackedWidget,
                               QVBoxLayout, QWidget)

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
        from .ui_kit import CustomCheckBox, SegmentButton, SmoothScrollArea, Theme, compute_scale, show_message
        self._last_page: dict[int, int] = {}

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

        # The sidebar: Main (your agents, their history, the on/off switch) and Settings (how they work:
        # models, functions, listening...; the app's look; the rest), each with its own pages below.
        self.sidebar = QWidget()
        side = QVBoxLayout(self.sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(max(4, pad // 2))
        self.stack = QStackedWidget()
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.modes = QButtonGroup(self)
        self.modes.setExclusive(True)
        from .icons import gear, main_icon
        text_color = theme.text()
        self.main_btn = SegmentButton(main_icon(64), position="top", text="Main")
        self.settings_btn = SegmentButton(gear(64, text_color), position="bottom", text="Settings")
        for i, b in enumerate((self.main_btn, self.settings_btn)):
            b.set_icon_target_size(26)
            b.setMinimumHeight(46)
            self.modes.addButton(b, i)
        modes_box = QVBoxLayout()
        modes_box.setSpacing(0)
        modes_box.addWidget(self.main_btn)
        modes_box.addWidget(self.settings_btn)
        side.addLayout(modes_box)
        side.addSpacing(pad // 2)
        self.navs = QStackedWidget()
        side.addWidget(self.navs, 1)

        send = self.send
        self.agents_page = AgentsPage(self.daemon)
        self.history_page = HistoryPage(self.daemon)
        layout_ = [
            ("main", [(None, [("Agents", self.agents_page), ("History", self.history_page)])]),
            ("settings", [
                ("Agents", [("Models", ModelsPage(self.daemon, send)), ("Functions", FunctionsPage(self.daemon, send)),
                            ("Listening", GeneralPage(self.daemon, send)), ("Accounts", AccountsPage(self.daemon, send)),
                            ("Dry Run", DryRunPage(self.daemon)), ("Training", TrainingPage(self.daemon, send))]),
                ("App", [("Appearance", AppearancePage(self.daemon, send)),
                         ("Indicators", IndicatorsPage(self.daemon, send))]),
                ("Miscellaneous", [("Wikipedia", WikipediaPage(self.daemon, send))]),
            ]),
        ]
        self.pages: list[tuple[str, Any]] = []
        self.first_page: dict[int, int] = {}
        for mode_i, (_mode, groups) in enumerate(layout_):
            col = QWidget()
            cl = QVBoxLayout(col)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(max(4, pad // 2))
            for group, pages in groups:
                if group:
                    head = QLabel(group.upper())
                    head.setStyleSheet(f"QLabel {{ color: {text_color.name()}; font-size: 10px; font-weight: bold; "
                                       "letter-spacing: 1px; padding: 6px 4px 0 4px; }}")
                    cl.addWidget(head)
                for title, page in pages:
                    i = len(self.pages)
                    self.pages.append((title, page))
                    self.first_page.setdefault(mode_i, i)
                    btn = SegmentButton(text=title, position="full")
                    btn.setMinimumHeight(36)
                    cl.addWidget(btn)
                    self.nav.addButton(btn, i)
                    self.stack.addWidget(page)
            cl.addStretch(1)
            if mode_i == 0:
                # the master switch: off = every AI stops and unloads (same as `jeeves --toggle`)
                self.power = CustomCheckBox("Jeeves on")
                self.power.setToolTip("Off stops the Jeeves daemon (every AI model, listening, keybinds); on starts "
                                      "a fresh one. Same as `jeeves --toggle`.")
                self.power.toggled.connect(self._power_toggled)
                cl.addWidget(self.power)
            # scrolls when the window is short, so the window can still shrink
            scroll = SmoothScrollArea()
            scroll.setWidget(col)
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.viewport().setAutoFillBackground(False)
            col.setAutoFillBackground(False)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            self.navs.addWidget(scroll)
        self._power_done.connect(self._after_power)
        self.status = QLabel("Connecting to the daemon…")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        side.addWidget(self.status)
        self.main_btn.setChecked(True)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self._open_page)
        self.modes.idClicked.connect(self._open_mode)

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

    def _open_page(self, i: int) -> None:
        from .ui_kit import crossfade_to_index
        crossfade_to_index(self.stack, i)
        self._refresh_page(i)

    def _open_mode(self, mode: int) -> None:
        """Main / Settings: their pages in the sidebar, and the first of them (or the last one used)."""
        self.navs.setCurrentIndex(mode)
        last = self._last_page.get(mode, self.first_page.get(mode, 0))
        cur = self.stack.currentIndex()
        self._last_page[1 - mode] = cur
        btn = self.nav.button(last)
        if btn is not None:
            btn.setChecked(True)
        self._open_page(last)

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
        if topic == "history" and page is self.history_page:
            page.refresh(self.settings, self.locked)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.sidebar.setFixedWidth(max(150, min(210, round(self.width() * 0.13))))
        self._compute_scale(self.width(), self.height())


if __name__ == "__main__":
    sys.exit(main())
