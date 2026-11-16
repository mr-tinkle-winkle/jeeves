"""Settings save as you go: your edits are saved a moment later; filling the form in never saves."""
import time

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait(qapp, pred, t=3.0):
    end = time.time() + t
    while time.time() < end:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeDaemon:
    def __init__(self, agents):
        self.agents = agents
        self.saves = []

    def call(self, method, cb=None, err=None, **k):
        res = None
        if method == "agents.save":
            self.saves.append((k["id"], k["agent"]))
        elif method == "functions.list":
            res = []
        elif method == "models.status":
            res = {"catalog": [], "kinds": {}}
        elif method == "audio.devices":
            res = {"devices": [], "outputs": []}
        if cb:
            cb(res)


def _page(qapp):
    from jeeves.daemon.engine import Engine
    from jeeves.gui import autosave
    from jeeves.gui.pages_agents import AgentsPage
    autosave.set_enabled(True)
    e = Engine(start_io=False)
    settings = {"agents": {aid: dict(a) for aid, a in e.agents().items()}}
    settings["agents"]["evil"] = dict(settings["agents"]["jeeves"], name="Evil Jeeves", call_names=["Evil Jeeves"])
    d = FakeDaemon(settings["agents"])
    p = AgentsPage(d, lambda *_a: None)
    p.resize(900, 700)
    p.show()
    p.refresh(settings, set())
    qapp.processEvents()
    qapp.processEvents()
    return p, d, settings


def test_filling_the_form_in_never_saves(qapp):
    p, d, _ = _page(qapp)
    p.select("evil")
    p.select("jeeves")
    _wait(qapp, lambda: False, 1.3)
    assert d.saves == []


def test_an_edit_is_saved_a_moment_later_without_asking(qapp):
    from PySide6.QtTest import QTest
    p, d, _ = _page(qapp)
    p.name.setFocus()
    p.name.selectAll()
    QTest.keyClicks(p.name, "Jeeves Prime")
    assert d.saves == []                               # (not on every key)
    assert _wait(qapp, lambda: d.saves, 3)
    aid, agent = d.saves[-1]
    assert aid == p.current and agent["name"] == "Jeeves Prime"
    assert "automatically" in p.saved_note.text()


def test_our_own_save_coming_back_doesnt_refill_the_form(qapp):
    from PySide6.QtTest import QTest
    p, d, settings = _page(qapp)
    QTest.keyClicks(p.name, " typed")
    assert _wait(qapp, lambda: d.saves, 3)
    QTest.keyClicks(p.name, " more")                   # still typing when the settings event arrives
    settings["agents"][p.current] = d.saves[-1][1]
    p.refresh(settings, set())
    assert p.name.text().endswith(" typed more")


def test_switching_agents_saves_the_one_you_were_on(qapp):
    from PySide6.QtTest import QTest
    p, d, _ = _page(qapp)
    first = p.current
    QTest.keyClicks(p.name, "X")
    p.select("evil" if first != "evil" else "jeeves")
    assert d.saves and d.saves[-1][0] == first


def test_autosave_off_means_save_button_only(qapp):
    from PySide6.QtTest import QTest
    from jeeves.gui import autosave
    p, d, _ = _page(qapp)
    autosave.set_enabled(False)
    try:
        QTest.keyClicks(p.name, "Y")
        _wait(qapp, lambda: False, 1.3)
        assert d.saves == []
    finally:
        autosave.set_enabled(True)


def test_several_devices_are_added_with_a_plus_and_saved(qapp):
    p, d, _ = _page(qapp)
    p._got_devices({"devices": [{"name": "mic.a", "description": "Headset mic", "kind": "input"},
                                {"name": "tv.monitor", "description": "TV", "kind": "output"},
                                {"name": "mic.b", "description": "Webcam mic", "kind": "input"}],
                    "outputs": [{"name": "sink.a", "description": "Headphones"},
                                {"name": "sink.b", "description": "TV speakers"}]})
    p.listen.setCurrentIndex(p.listen.findData("devices"))
    assert p.devices_row.isVisibleTo(p) and not p.device_row.isVisibleTo(p)
    assert p.devices_list.value() == ["mic.a"]                      # starts with one picker to fill in
    p.devices_list.add_btn.click()                                  # + : the next device not taken yet
    p.devices_list.add_btn.click()
    assert p.devices_list.value() == ["mic.a", "tv.monitor", "mic.b"]
    r, _box = p.devices_list.rows[1]
    r.findChildren(type(p.devices_list.add_btn))[0].click()         # × on the second
    assert p.devices_list.value() == ["mic.a", "mic.b"]
    p.output.setCurrentIndex(p.output.findData("devices_mic"))
    assert p.out_devices_row.isVisibleTo(p)
    p.out_devices_list.add_btn.click()
    assert p.out_devices_list.value() == ["sink.a", "sink.b"]
    got = p._collect()
    assert got["listen_to"] == "devices" and got["listen_devices"] == ["mic.a", "mic.b"]
    assert got["output_to"] == "devices_mic" and got["output_devices"] == ["sink.a", "sink.b"]
    assert _wait(qapp, lambda: d.saves, 3)                          # adding and removing count as edits
    # a saved list comes back as it was, an unplugged device marked
    p.agents[p.current] = dict(got, listen_devices=["mic.b", "gone.mic"])
    p.select(p.current)
    qapp.processEvents()
    assert p.devices_list.value() == ["mic.b", "gone.mic"]
    assert "not connected" in p.devices_list.rows[1][1].currentText()
