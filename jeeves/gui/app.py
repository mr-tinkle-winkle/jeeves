"""The Jeeves settings window: a front-end only. Every change is a request to
the daemon; the window works on whatever the daemon reports back."""
from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

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
    for win in _windows:
        win.daemon.stop()
    return code


_windows: list["MainWindow"] = []


class MainWindow(QMainWindow):
    """Agents down the side; an agent's own tabs (overview, personality, voice, brain, abilities,
    sources, history) on the right; Activity, Models and Settings shared by all of them."""
    _power_done = Signal(str)          # from the thread that starts/stops the daemon

    AGENT_TABS = ("overview", "personality", "voice", "brain", "abilities", "sources", "history")

    def __init__(self) -> None:
        super().__init__()
        from .pages_agents import AgentsPage
        from .pages_functions import FunctionsPage
        from .pages_misc import (AccountsPage, DryRunPage, GeneralPage, HistoryPage, IndicatorsPage, TrainingPage,
                                 WikipediaPage)
        from .pages_models import ModelsPage
        from .shell import AgentHeader, AgentHistoryPage, LookPage, OverviewPage, Sidebar, ThemedTabs
        from .themes import words
        from .ui_kit import CustomCheckBox, Theme, compute_scale, show_message
        _windows.append(self)
        self._show_message = show_message
        self._compute_scale = compute_scale
        self.setWindowTitle("Jeeves")
        self.resize(1280, 820)
        self.daemon = Daemon()
        self.settings: dict[str, Any] = {}
        self.locked: set[str] = set()
        self.stages: dict[str, dict[str, str]] = {}      # agent -> request -> stage (the indicators)
        theme = self.theme = Theme()
        w = self.words = words(theme.settings.vocab)

        central = QWidget()
        self.setCentralWidget(central)
        theme_palette(central)
        rowl = QHBoxLayout(central)
        pad = theme.padding
        rowl.setContentsMargins(pad, pad, pad, pad)
        rowl.setSpacing(pad)

        # ---- the sidebar
        self.sidebar = Sidebar()
        self.sidebar.chosen.connect(self._chosen)
        send = self.send
        self.agents_page = AgentsPage(self.daemon, embedded=True)
        self.sidebar.add_agent.connect(self.agents_page.add_agent)
        self.power = CustomCheckBox("Jeeves on")
        self.power.setToolTip("Off stops the Jeeves daemon (every AI model, listening, keybinds); on starts "
                              "a fresh one. Same as `jeeves --toggle`.")
        self.power.toggled.connect(self._power_toggled)
        self.sidebar.footer.addWidget(self.power)
        self.status = QLabel("Connecting to the daemon…")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet(f"QLabel {{ color: {theme.muted().name()}; }}")
        self.sidebar.footer.addWidget(self.status)

        # ---- an agent: header, tabs, and what each tab shows
        self.agent_view = QWidget()
        av = QVBoxLayout(self.agent_view)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(max(6, pad // 2))
        self.header = AgentHeader()
        self.header.talk.connect(self._talk)
        av.addWidget(self.header)
        self.agent_tabs = ThemedTabs([(k, w[k]) for k in self.AGENT_TABS])
        self.agent_tabs.picked.connect(self._agent_tab)
        av.addWidget(self.agent_tabs)
        self.agent_stack = QStackedWidget()
        self.overview = OverviewPage(self.daemon)
        self.overview.open_tab.connect(self._open_agent_tab)
        self.agent_history = AgentHistoryPage(self.daemon)
        for page in (self.overview, self.agents_page, self.agent_history):
            self.agent_stack.addWidget(page)
        av.addWidget(self.agent_stack, 1)
        self.agents_page.on_loaded = self._update_overview
        self.agent_tab = "overview"
        self.aid = ""

        # ---- settings every agent shares
        self.settings_view = QWidget()
        sv = QVBoxLayout(self.settings_view)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(max(6, pad // 2))
        head = QLabel(theme.heading(w["settings"]))
        head.setFont(theme.display_font(26, 700))
        head.setStyleSheet(f"QLabel {{ color: {theme.text().name()}; padding: 4px 6px; }}")
        sv.addWidget(head)
        self.settings_pages = [
            ("listening", "Listening & keys", GeneralPage(self.daemon, send)),
            ("functions", "Functions", FunctionsPage(self.daemon, send)),
            ("look", "Look", LookPage(self.daemon, self.rebuild)),
            ("indicators", "Indicators", IndicatorsPage(self.daemon, send)),
            ("accounts", "Accounts", AccountsPage(self.daemon, send)),
            ("dryrun", "Dry run", DryRunPage(self.daemon)),
            ("training", "Training", TrainingPage(self.daemon, send)),
            ("wikipedia", "Wikipedia", WikipediaPage(self.daemon, send)),
        ]
        self.settings_tabs = ThemedTabs([(k, t) for k, t, _p in self.settings_pages])
        self.settings_tabs.picked.connect(self._settings_tab)
        sv.addWidget(self.settings_tabs)
        self.settings_stack = QStackedWidget()
        for _k, _t, page in self.settings_pages:
            self.settings_stack.addWidget(page)
        sv.addWidget(self.settings_stack, 1)

        self.history_page = HistoryPage(self.daemon)
        self.models_page = ModelsPage(self.daemon, send)
        self.stack = QStackedWidget()
        for v in (self.agent_view, self.history_page, self.models_page, self.settings_view):
            self.stack.addWidget(v)
        # every page, for whatever wants to visit them all
        self.pages: list[tuple[str, Any]] = [("Agent", self.overview), ("Agent settings", self.agents_page),
                                             ("Agent history", self.agent_history), ("Activity", self.history_page),
                                             ("Models", self.models_page)] + \
            [(t, p) for _k, t, p in self.settings_pages]

        rowl.addWidget(self.sidebar)
        rowl.addWidget(self.stack, 1)
        self._power_done.connect(self._after_power)
        self.daemon.connected.connect(self._connected)
        self.daemon.event.connect(self._event)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(150)
        self._reload_timer.timeout.connect(self.reload)

    # ------------------------------------------------------------------ navigation
    def _chosen(self, kind: str, key: str) -> None:
        from .motion import switch
        if kind == "agent":
            switch(self.stack, 0)
            self.select_agent(key)
        else:
            switch(self.stack, {"activity": 1, "models": 2, "settings": 3}[key])
            self._refresh_current()

    def select_agent(self, aid: str) -> None:
        agents = self.settings.get("agents") or {}
        if aid not in agents:
            return
        first = aid != self.aid
        self.aid = aid
        self.sidebar.pick("agent", aid, emit=False)
        index = list(agents).index(aid)
        self.header.set_agent(aid, agents[aid], index)
        self.header.set_stage(self._stage(aid))
        self.agents_page.current = aid
        self.agents_page.select(aid)
        self.agent_history.aid = aid
        self._update_overview(animate=first)
        if first:
            self._open_agent_tab(self.agent_tab, force=True)

    def _open_agent_tab(self, tab: str, force: bool = False) -> None:
        self.agent_tabs.set_current(tab)
        self._agent_tab(tab, force)

    def _agent_tab(self, tab: str, force: bool = False) -> None:
        from .motion import enter, switch
        changed = tab != self.agent_tab
        self.agent_tab = tab
        if tab == "overview":
            target = self.overview
        elif tab == "history":
            target = self.agent_history
            self.agent_history.show_agent(self.aid)
        else:
            target = self.agents_page
            self.agents_page.show_tab(tab)
        if self.agent_stack.currentWidget() is target:
            if changed or force:
                enter(target)
        else:
            switch(self.agent_stack, self.agent_stack.indexOf(target))

    def _settings_tab(self, key: str) -> None:
        from .motion import switch
        i = next(i for i, (k, _t, _p) in enumerate(self.settings_pages) if k == key)
        switch(self.settings_stack, i)
        self._refresh_current()

    def current_page(self) -> Any:
        v = self.stack.currentWidget()
        if v is self.agent_view:
            return self.agent_stack.currentWidget()
        if v is self.settings_view:
            return self.settings_stack.currentWidget()
        return v

    def show_page(self, page: Any) -> None:
        """Open any page (tests and scripts visit them all)."""
        from .motion import switch
        if page in (self.overview, self.agents_page, self.agent_history):
            switch(self.stack, 0)
            tab = {id(self.overview): "overview", id(self.agent_history): "history"}.get(id(page), "personality")
            self._open_agent_tab(tab)
        elif page is self.history_page:
            switch(self.stack, 1)
        elif page is self.models_page:
            switch(self.stack, 2)
        else:
            switch(self.stack, 3)
            key = next(k for k, _t, p in self.settings_pages if p is page)
            self.settings_tabs.set_current(key)
            self._settings_tab(key)
        self._refresh_current()

    def _refresh_current(self) -> None:
        page = self.current_page()
        if self.settings and page is not None and page not in (self.overview,):
            page.refresh(self.settings, self.locked)

    def _update_overview(self, animate: bool = False) -> None:
        agents = self.settings.get("agents") or {}
        if self.aid in agents:
            self.overview.show_agent(self.aid, agents[self.aid], self.settings, self.agents_page.catalog,
                                     self.agents_page.functions, animate)

    def _talk(self) -> None:
        if self.aid:
            self.daemon.call("request.voice", None, lambda e: self._show_message(self, "Talk", e), agent=self.aid)

    # ------------------------------------------------------------------ live state
    def _stage(self, aid: str) -> str:
        stages = list((self.stages.get(aid) or {}).values())
        for want in ("responding", "researching", "thinking", "watching", "asking", "transcript", "listening"):
            if want in stages:
                return want
        return stages[-1] if stages else ""

    def _indicator(self, st: Any) -> None:
        if not isinstance(st, dict) or not st.get("agent"):
            return
        aid, rid, stage = st["agent"], st.get("request") or "", st.get("stage") or "idle"
        per = self.stages.setdefault(aid, {})
        if stage in ("idle", "unavailable", "unclear"):
            per.pop(rid, None)
        else:
            per[rid] = stage
        now = self._stage(aid)
        self.sidebar.set_stage(aid, now)
        if aid == self.aid:
            self.header.set_stage(now)

    # ------------------------------------------------------------------ theme
    def rebuild(self) -> None:
        """The theme changed: build the window again in it, where this one was, and close this one."""
        from .common import apply_app_font
        apply_app_font()
        new = MainWindow()
        new.setGeometry(self.geometry())
        new.aid = ""
        new._pending = (self.aid, "settings", "look")
        new.show()
        self.daemon.stop()
        self.close()
        if self in _windows:
            _windows.remove(self)
        self.deleteLater()

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
            agents = self.settings.get("agents") or {}
            self.sidebar.set_agents(agents)
            for aid in agents:
                self.sidebar.set_stage(aid, self._stage(aid))
            self.agents_page.refresh(self.settings, self.locked)
            pending = getattr(self, "_pending", None)
            if pending:
                self._pending = None
                self.sidebar.pick("nav", "settings", emit=False)
                self.stack.setCurrentIndex(3)
                self.settings_tabs.set_current("look", animate=False)
                self.settings_stack.setCurrentIndex(2)
                self.aid = ""
                if pending[0] in agents:
                    self.aid = pending[0]
                    self.header.set_agent(self.aid, agents[self.aid], list(agents).index(self.aid))
                    self.agents_page.current = self.aid
            elif self.stack.currentIndex() == 0 and (self.aid not in agents):
                want = self.agents_page.current if self.agents_page.current in agents else next(iter(agents), "")
                if want:
                    self.aid = ""
                    self.select_agent(want)
            elif self.aid in agents:
                self.header.set_agent(self.aid, agents[self.aid], list(agents).index(self.aid))
                if self.stack.currentIndex() == 0:
                    self.sidebar.pick("agent", self.aid, emit=False)
                self._update_overview()
            if self.stack.currentIndex() != 0 or self.agent_stack.currentWidget() is not self.agents_page:
                self._refresh_current()
        self.daemon.call("settings.get", got, lambda _e: None)
        self.daemon.call("indicator.state", self._states, lambda _e: None)

    def _states(self, res: Any) -> None:
        self.stages = {}
        for st in res or []:
            self._indicator(st)

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
        elif topic == "indicator":
            self._indicator(data)
        elif topic in ("functions", "import_done"):
            self._refresh_current()
        elif topic == "history":
            self.overview.refresh_recent()
            if self.current_page() in (self.history_page, self.agent_history):
                self.current_page().refresh(self.settings, self.locked)
        page = self.current_page()
        handler = getattr(page, "on_event", None)
        if handler:
            handler(topic, data)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.sidebar.setFixedWidth(max(190, min(250, round(self.width() * 0.18))))
        self._compute_scale(self.width(), self.height())


if __name__ == "__main__":
    sys.exit(main())
