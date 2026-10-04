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
