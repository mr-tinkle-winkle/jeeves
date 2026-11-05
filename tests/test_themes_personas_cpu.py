"""Themes and the new window, built-in personalities, and the CPU fixes and report."""
import json
import subprocess
import threading
import time
from dataclasses import asdict

import pytest


# ---------------------------------------------------------------------------- personalities
def test_personalities_come_in_strengths_and_light_means_light(engine):
    from jeeves.models import persona
    from jeeves.personas import PERSONAS, STRENGTHS
    butler = PERSONAS["butler"]
    texts = [butler.prompt(s) for s in STRENGTHS]
    assert len(set(texts)) == 3
    assert "tea" in texts[0] and "no tea" in texts[0]           # light: no going on about the tea
    agent = dict(engine.agents()["jeeves"], prompt=texts[0], persona_strength="light")
    block = persona.identity_block(agent)
    assert "light" in block and "every reply" not in block
    agent["persona_strength"] = None                            # older agents: as before, fully in character
    assert "every reply" in persona.identity_block(agent)


def test_the_agent_form_fills_in_a_personality(qapp_or_skip, engine):
    from jeeves.gui.pages_agents import AgentsPage

    class D:
        def call(self, *a, **k):
            pass
    page = AgentsPage(D(), embedded=True)
    page.refresh(engine.settings.effective(), set())
    page.select("jeeves")
    page.persona.setCurrentIndex(page.persona.findData("butler"))
    page.strength.setCurrentIndex(page.strength.findData("light"))
    page._apply_persona()
    a = page._collect()
    assert "no tea" in a["prompt"] and a["persona_preset"] == "butler" and a["persona_strength"] == "light"


# ---------------------------------------------------------------------------- themes
@pytest.fixture
def qapp_or_skip():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("key", ["ledger", "alive", "console", "brutalist", "arcade", "blueprint"])
def test_every_theme_builds_the_window_and_every_page(qapp_or_skip, engine, key):
    from jeeves import paths
    from jeeves.gui.common import apply_app_font, install_theme
    from jeeves.gui.themes import LOOKS
    data = json.loads(paths.settings_file().read_text()) if paths.settings_file().exists() else {}
    data["theme"] = asdict(LOOKS[key].settings())
    paths.settings_file().write_text(json.dumps(data))
    install_theme()
    apply_app_font()
    from jeeves.gui.app import MainWindow
    from jeeves.gui.ui_kit import get_settings
    assert get_settings().preset == key
    w = MainWindow()
    w.settings = engine.settings.effective()
    w.sidebar.set_agents(w.settings["agents"])
    w.show()
    w.select_agent("jeeves")
    w._indicator({"agent": "jeeves", "request": "r1", "stage": "responding"})
    assert w.sidebar.rows[("agent", "jeeves")].active() and w.header.live()
    for tab in w.AGENT_TABS:
        w._open_agent_tab(tab)
        qapp_or_skip.processEvents()
    for _t, page in w.pages:
        w.show_page(page)
        qapp_or_skip.processEvents()
    img = w.grab()
    assert not img.isNull()
    w._indicator({"agent": "jeeves", "request": "r1", "stage": "idle"})
    assert not w.header.live()
    w.close()
    w.daemon.stop()


def test_tabs_move_their_marker_the_themes_way(qapp_or_skip):
    from jeeves.gui import motion
    assert motion.stepped(0.3) == 0.25 and motion.stepped(0.99) == 0.75 and motion.stepped(1.0) == 1.0
    ms, curve = motion.timing("spring")
    assert curve.overshoot() > 1 and ms > motion.timing("slam")[0]


# ---------------------------------------------------------------------------- CPU
def test_kde_window_queries_are_few_and_shared(monkeypatch):
    from jeeves.daemon import desktop as dk
    calls = []
    wins = {"1": "firefox", "2": "steam", "3": "konsole"}

    def kdo(*args):
        calls.append(args[0])
        if args[0] == "getactivewindow":
            return "2"
        if args[0] == "search":
            return "\n".join(wins)
        if args[0] == "getwindowclassname":
            return wins[args[1]]
        if args[0] == "getwindowname":
            return "title"
        if args[0] == "getwindowgeometry":
            return "Position: 0,0\nGeometry: 800x600"
        return ""
    monkeypatch.setattr(dk, "_kdo", kdo)
    monkeypatch.setattr(dk, "desktop", lambda: "kde")
    dk._cache.clear()
    assert dk.focused().app == "steam"
    assert calls.count("search") == 0 and len(calls) <= 5          # just the focused window, not all of them
    calls.clear()
    assert dk.open_apps() == ["firefox", "konsole", "steam"]
    assert calls == ["search"] + ["getwindowclassname"] * 3          # only the classes
    calls.clear()
    dk.open_apps(), dk.focused()
    assert calls == []                                               # asked again straight away: shared
    dk._cache.clear()


def test_the_cpu_report_names_the_programs_started_and_who_started_them():
    from jeeves.daemon import cpuwatch

    def busy():
        for _ in range(3):
            subprocess.run(["true"])
            time.sleep(0.1)
    t = threading.Timer(0.2, busy)
    t.start()
    rep = cpuwatch.profile(1.5, interval=0.05)
    t.join()
    started = {s["program"]: s for s in rep["started"]}
    assert started["true"]["times"] == 3 and "busy" in started["true"]["from"][0][0]
    text = cpuwatch.format_report(rep)
    assert "Programs it started" in text and "true ×3" in text
