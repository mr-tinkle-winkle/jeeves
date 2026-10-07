"""Offscreen GUI checks (skipped in the Nix build: -k 'not gui')."""
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    QCursor.setPos(4000, 4000)
    return app


def test_gui_builds_every_page_without_unscoped_stylesheets(qapp):
    from jeeves.gui.common import install_theme
    install_theme()
    from jeeves.gui.app import MainWindow
    from PySide6.QtWidgets import QWidget
    w = MainWindow()
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()
    bad = []
    for child in w.findChildren(QWidget):
        sheet = child.styleSheet().strip()
        if sheet and not sheet.split("{")[0].strip():
            bad.append(type(child).__name__)
    assert not bad, f"unscoped stylesheets on {bad}"
    # every page scrolls, so the window can shrink (UI guide pitfall 4)
    assert w.minimumSizeHint().height() < 400
    for i in range(len(w.pages)):
        w.nav.button(i).click()
        qapp.processEvents()
    w.close()
    w.daemon.stop()


def test_settings_binder_locks(qapp):
    from jeeves.gui.widgets import Binder
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    sent = []
    host = QWidget()
    lay = QVBoxLayout(host)
    b = Binder(lambda changes: sent.append(changes))
    cb = b.check(lay, "Summary", "summary.enabled")
    num = b.number(lay, "Minutes", "summary.minutes", 1, 100)
    b.load({"summary": {"enabled": True, "minutes": 30}}, {"summary.minutes"})
    assert cb.isChecked() and cb.isEnabled()
    assert num.value() == 30 and not num.isEnabled()
    assert sent == []                 # loading never echoes back as a change
    cb.setChecked(False)
    assert sent == [{"summary.enabled": False}]


def test_models_page_shows_hybrid_models_and_the_vision_pick(qapp, engine):
    from jeeves.gui.pages_models import ModelsPage

    class D:
        def call(self, *a, **k):
            pass
    page = ModelsPage(D(), lambda changes: None)
    st = engine.models.status()
    st["hybrid"] = {"enabled": True, "light": True, "reason": "the GPU is 90% busy", "usage": {}}
    for m in st["catalog"]:
        m["installed"] = m["id"] in ("qwen3-8b", "qwen3-1.7b", "qwen2.5-vl-3b")
    page._settings = engine.settings.effective()
    page._got(st)
    assert "light models" in page.hybrid_status.text() and "90%" in page.hybrid_status.text()
    items = [page.light_boxes["local_response"].itemData(i) for i in range(page.light_boxes["local_response"].count())]
    assert items[:2] == ["auto", "same"] and "qwen3-1.7b" in items and "qwen2.5-vl-3b" not in items
    vis = [page.light_boxes["vision"].itemData(i) for i in range(page.light_boxes["vision"].count())]
    assert "off" in vis and "qwen2.5-vl-3b" in vis
    page._got(st)                                 # filling twice doesn't duplicate entries
    assert page.light_boxes["local_response"].count() == len(items)
    page._got_recs(engine.models.recommend())


def test_the_logo_ships_in_every_size(qapp):
    from jeeves.resources import ICON_SIZES, app_icon, icon_path
    from PySide6.QtGui import QImage
    for s in ICON_SIZES:
        img = QImage(str(icon_path(s)))
        assert img.width() == s and img.height() == s and img.hasAlphaChannel()
    assert icon_path(20).name == "jeeves-22.png" and icon_path(1000).name == "jeeves-512.png"
    assert not app_icon().isNull() and len(app_icon().availableSizes()) == len(ICON_SIZES)


def test_command_editor_round_trip(qapp):
    from jeeves.gui.common import install_theme
    install_theme()
    from jeeves.gui.command_editor import CommandList
    lst = CommandList()
    spec = {"name": "rebuild", "description": "Rebuilds NixOS", "command": "nixos-rebuild switch --flake ~/nix#{host}",
            "args": [{"name": "host", "description": "which machine", "choices": ["desk", "laptop"],
                      "required": False}], "confirm": True, "terminal": True, "timeout": 900}
    lst.load([spec])
    assert lst.value() == [spec]
    card = lst._cards[0]
    card.command.setText("nix-push {message} {branch}")
    got = lst.value()[0]
    assert [a["name"] for a in got["args"]] == ["message", "branch"]
    card.command.setText("nixos-rebuild switch --flake ~/nix#{host}")
    assert lst.value()[0]["args"][0]["description"] == "which machine"     # remembered while typing


def test_agents_page_keeps_commands_jeenius_and_custom_sources(qapp):
    from jeeves.config import default_agent
    from jeeves.gui.common import install_theme
    install_theme()
    from jeeves.gui.pages_agents import AgentsPage

    class Daemon:
        def call(self, *a, **kw):
            pass
    page = AgentsPage(Daemon())
    agent = default_agent("Jeeves")
    agent.update(jeenius=3, custom_sources={"enabled": True, "sites": [
        {"url": "https://parkour-reborn.fandom.com/wiki/Movement",
         "about": "Parkour Reborn Movement Wiki, for information on any movement techniques"}]})
    page.agents = {"jeeves": agent}
    page.picker.addItem("Jeeves", "jeeves")
    page.select("jeeves")
    got = page._collect()
    assert got["jeenius"] == 3 and got["custom_sources"] == agent["custom_sources"]
    page.sources_edit.setPlainText(page.sources_edit.toPlainText() + "\nwhite-knuckle.wiki.gg = White Knuckle wiki\nnope")
    assert "line 3 has no web address" in page.sources_status.text().lower()
    sites = page._collect()["custom_sources"]["sites"]
    assert [s["url"] for s in sites] == ["https://parkour-reborn.fandom.com/wiki/Movement",
                                         "https://white-knuckle.wiki.gg"]
    page.sources_on.setChecked(False)
    assert page._collect()["custom_sources"]["enabled"] is False
