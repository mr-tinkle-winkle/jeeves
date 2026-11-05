"""YouTube's stream proxy, rudimentary agents, an agent's own on/off switch, the themed controls,
indicators and window frame, and hover highlights."""
import inspect
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------- the stream proxy
class _Picky(BaseHTTPRequestHandler):
    """Like YouTube: only with the right User-Agent, and only pieces of at most LIMIT bytes."""
    protocol_version = "HTTP/1.1"
    data = bytes(range(256)) * 2000                  # 512 000 bytes
    LIMIT = 100_000
    seen: list = []

    def log_message(self, *_a):
        pass

    def do_GET(self):
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        if self.headers.get("User-Agent") != "YT" or not m:
            return self._bare(403)
        a = int(m.group(1))
        b = min(int(m.group(2)) if m.group(2) else len(self.data) - 1, len(self.data) - 1)
        if b - a + 1 > self.LIMIT:
            return self._bare(403)
        if a >= len(self.data):
            return self._bare(416)
        self.seen.append((a, b))
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {a}-{b}/{len(self.data)}")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        self.wfile.write(self.data[a:b + 1])

    def _bare(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def picky():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Picky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Picky.seen = []
    yield f"http://127.0.0.1:{srv.server_address[1]}/videoplayback?id=1"
    srv.shutdown()


def test_the_player_gets_youtube_streams_through_the_proxy(picky, monkeypatch):
    from jeeves.daemon import streamproxy
    monkeypatch.setattr(streamproxy, "CHUNK", 100_000)
    with pytest.raises(urllib.error.HTTPError):       # what the player saw: "Could not open file"
        urllib.request.urlopen(picky, timeout=5)
    local = streamproxy.register(picky, {"User-Agent": "YT", "Accept": "*/*"}, "video/mp4")
    assert local.startswith("http://127.0.0.1:")
    body = urllib.request.urlopen(local, timeout=10).read()
    assert body == _Picky.data and len(_Picky.seen) >= 6           # fetched in pieces
    req = urllib.request.Request(local, headers={"Range": "bytes=250000-250009"})
    with urllib.request.urlopen(req, timeout=10) as r:              # seeking
        assert r.status == 206 and r.read() == _Picky.data[250000:250010]
        assert r.headers["Content-Range"] == f"bytes 250000-250009/{len(_Picky.data)}"
    assert urllib.request.urlopen(urllib.request.Request(local, method="HEAD"), timeout=5).headers[
        "Content-Length"] == str(len(_Picky.data))
    with pytest.raises(urllib.error.HTTPError):                     # only what was registered
        urllib.request.urlopen(local.rsplit("/", 1)[0] + "/nope", timeout=5)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_ffmpeg_plays_and_seeks_through_the_proxy(picky, monkeypatch, tmp_path):
    from jeeves.daemon import streamproxy
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=15",
                    "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    monkeypatch.setattr(_Picky, "data", clip.read_bytes())
    monkeypatch.setattr(_Picky, "LIMIT", 20_000)
    monkeypatch.setattr(streamproxy, "CHUNK", 20_000)
    local = streamproxy.register(picky, {"User-Agent": "YT"})
    direct = subprocess.run(["ffmpeg", "-v", "error", "-i", picky, "-f", "null", "-"], capture_output=True)
    assert direct.returncode != 0
    for extra in ([], ["-ss", "4"]):
        out = subprocess.run(["ffmpeg", "-v", "error", *extra, "-i", local, "-f", "null", "-"], capture_output=True,
                             timeout=60)
        assert out.returncode == 0, out.stderr


def test_youtube_formats_go_through_the_proxy_but_playlists_dont():
    from jeeves.functions.partials import youtube as yt
    assert yt.playable({"url": "https://x/videoplayback", "protocol": "https", "http_headers": {"User-Agent": "a"},
                        "ext": "mp4", "vcodec": "avc1"}).startswith("http://127.0.0.1:")
    assert yt.playable({"url": "https://x/index.m3u8", "protocol": "m3u8_native"}) == "https://x/index.m3u8"
    assert "[protocol^=http]" in yt.FORMAT.format(h=1080) and yt.FORMAT.format(h=720).endswith("/b")


# ---------------------------------------------------------------------------- rudimentary agents
class _LLM:
    def __init__(self, answers):
        self.answers, self.seen, self.kw = list(answers), [], []

    def loaded(self):
        return True

    def chat(self, messages, **kw):
        self.seen.append(messages)
        self.kw.append(kw)
        return self.answers.pop(0)


class _Models:
    def __init__(self, llm):
        self._llm, self.suspended = llm, {}

    def llm(self, kind, agent=None):
        return self._llm


def test_a_rudimentary_agent_only_works_out_how_to_run_its_one_function(engine):
    from jeeves.daemon.intent import IntentProcessor
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": True, "function": "timers"})
    agent = engine.agents()["jeeves"]
    assert [f.name for f in engine.registry.enabled_for(agent)] == ["timers"]
    llm = _LLM(['{"args": {"action": "timer", "duration": "ten minutes", "label": "tea"}}'])
    ip = IntentProcessor(engine.settings, engine.registry, _Models(llm), engine.history)
    d = ip.decide(agent, "ten minutes for the tea, please")
    assert d.function == "timers" and d.args["duration"] == "ten minutes" and d.method == "model"
    system = llm.seen[0][0]["content"]
    normal = dict(agent, rudimentary={})
    full = ip.system_prompt(normal, engine.registry.enabled_for(normal))
    assert "### timers" in system and "### research" not in system and len(system) < len(full) / 3
    assert llm.kw[0]["think"] is False and llm.kw[0]["max_tokens"] <= 200      # no thinking, a short answer
    # missing something: it asks for just that, it never falls back to chatting
    llm = _LLM(['{"question": "How long?"}'])
    ip = IntentProcessor(engine.settings, engine.registry, _Models(llm), engine.history)
    d = ip.decide(agent, "do the thing")
    assert d.function is None and d.question == "How long?"


def test_a_rudimentary_agent_with_nothing_to_fill_in_needs_no_model(engine):
    from jeeves.daemon.intent import IntentProcessor
    engine.settings.set("agents.jeeves.commands", [{"name": "lights", "command": "true", "confirm": False}])
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": True, "function": "lights"})
    ip = IntentProcessor(engine.settings, engine.registry, _Models(_LLM([])), engine.history)   # (no answers)
    d = ip.decide(engine.agents()["jeeves"], "anything at all")
    assert d.function == "lights" and d.method == "rules"
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    res = engine.history.get(engine.handle_text("Jeeves, go", wait=True)["id"])
    assert res["function"] == "lights" and res["status"] == "done"


def test_without_a_model_a_rudimentary_agent_uses_keywords_and_asks_instead_of_chatting(engine):
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": True, "function": "timers"})
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    res = engine.history.get(engine.handle_text("Jeeves, set a timer for 2 minutes", wait=True)["id"])
    assert res["function"] == "timers" and res["status"] == "done"
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": False, "function": "timers"})
    assert len(engine.registry.enabled_for(engine.agents()["jeeves"])) > 1      # off: a normal agent again


# ---------------------------------------------------------------------------- the GUI
@pytest.fixture
def themed(engine, qapp_or_skip):
    from jeeves import paths
    from jeeves.gui.common import apply_app_font, install_theme
    from jeeves.gui.themes import LOOKS

    def use(key):
        import json
        data = json.loads(paths.settings_file().read_text()) if paths.settings_file().exists() else {}
        data["theme"] = asdict(LOOKS[key].settings())
        paths.settings_file().write_text(json.dumps(data))
        install_theme()
        apply_app_font()
    return use


@pytest.fixture
def qapp_or_skip():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_an_agent_switches_on_and_off_from_its_header_and_its_menu(engine, themed, qapp_or_skip):
    themed("alive")
    from jeeves.gui.app import MainWindow
    w = MainWindow()
    w.settings = engine.settings.effective()
    sent = []
    w.send = lambda changes: sent.append(changes)
    w.sidebar.set_agents(w.settings["agents"])
    w.show()
    w.select_agent("jeeves")
    w.header.switch.click()
    assert sent[-1] == {"agents.jeeves.enabled": False}
    assert not w.sidebar.rows[("agent", "jeeves")].on and not w.header.button.isEnabled()
    row = w.sidebar.rows[("agent", "jeeves")]
    w.sidebar._menu(row, row.rect().center())
    acts = [a for a in w.sidebar._open_menu.actions() if a.text()]
    assert [a.text() for a in acts] == ["Talk — Jeeves", "Turn Jeeves on", "Open Jeeves"] and not acts[0].isEnabled()
    acts[1].trigger()
    assert sent[-1] == {"agents.jeeves.enabled": True} and w.header.switch.isChecked() and w.header.button.isEnabled()
    w.sidebar._open_menu.close()
    w.close()
    w.daemon.stop()


@pytest.mark.parametrize("key", ["classic", "alive", "ledger", "console", "brutalist", "arcade", "blueprint"])
def test_every_control_is_drawn_the_themes_way(themed, qapp_or_skip, key):
    themed(key)
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from jeeves.gui.ui_kit import (CustomButton, CustomCheckBox, CustomGroupBox, CustomLineEdit, CustomPlainTextEdit,
                                   CustomRadioButton, CustomSlider, CustomSpinBox, skin)
    from jeeves.gui.widgets import combo
    page = QWidget()
    lay = QVBoxLayout(page)
    box = CustomGroupBox("Identity")
    inner = box.make_layout()
    widgets = [CustomCheckBox("On"), CustomRadioButton("One"), CustomLineEdit("text"), CustomSpinBox(),
               CustomPlainTextEdit("lines"), CustomSlider(), CustomButton("Save agent"), combo()]
    widgets[-1].addItems(["a", "b"])
    for x in widgets:
        inner.addWidget(x)
    lay.addWidget(box)
    page.resize(500, 520)
    page.show()
    widgets[0].setChecked(True)
    qapp_or_skip.processEvents()
    assert not page.grab().isNull()
    assert skin.kind(box._theme) == {"classic": "fade", "alive": "spring", "ledger": "glide", "console": "type",
                                     "brutalist": "slam", "arcade": "step", "blueprint": "draw"}[key]
    hint = widgets[6].sizeHint()
    assert hint.width() > 60 and hint.height() >= 20
    if key == "console":
        assert skin.button_label(box._theme, "Save agent") == "[ save agent ]"
    page.close()


@pytest.mark.parametrize("key", ["classic", "alive", "arcade"])
def test_the_window_draws_its_own_rounded_frame(engine, themed, qapp_or_skip, key):
    themed(key)
    from PySide6.QtCore import Qt

    from jeeves.gui.app import MainWindow
    w = MainWindow()
    assert w.windowFlags() & Qt.FramelessWindowHint
    w.show()
    img = w.grab().toImage()
    assert img.pixelColor(0, 0).alpha() == 0                        # the corner is see-through
    assert img.pixelColor(w.width() // 2, w.height() // 2).alpha() == 255
    w.close()
    w.daemon.stop()


def test_the_indicators_are_drawn_in_every_theme(themed, qapp_or_skip):
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    from jeeves import overlay_skin as ovs
    from jeeves.gui.ui_kit import Theme
    for key in ["classic", "alive", "ledger", "console", "brutalist", "arcade", "blueprint"]:
        themed(key)
        t = Theme()
        img = QImage(400, 200, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        ovs.mic(p, QRectF(4, 4, 56, 56), t, 0.3, True, False)
        ovs.spinner(p, QRectF(70, 4, 56, 56), t, QColor("#000000"), 0.4, True, QColor("#ffffff"), False)
        ovs.box(p, QRectF(140, 4, 200, 30), t, big=True)
        ovs.timer_bar(p, QRectF(140, 50, 200, 4), t, 0.5)
        ovs.mark(p, QPointF(300, 140), 30, t, 0.2)
        p.end()
        assert img.pixelColor(98, 32).alpha() > 0, key              # the spinner shows something


def test_custom_painted_hover_highlights_repaint_on_enter_and_leave():
    """Widgets that paint a hover highlight must repaint when the mouse enters and leaves them --
    without it, only the window's first repaint showed one (the sidebar's rows)."""
    from jeeves.gui import shell, widgets
    from jeeves.gui import ui_kit
    from PySide6.QtWidgets import QWidget
    missing = []
    for mod in [shell, widgets] + [getattr(ui_kit, n) for n in dir(ui_kit) if inspect.ismodule(getattr(ui_kit, n))]:
        for _n, cls in inspect.getmembers(mod, inspect.isclass):
            if not issubclass(cls, QWidget) or not cls.__module__.startswith("jeeves"):
                continue
            paint = cls.__dict__.get("paintEvent")
            if paint is None or "underMouse" not in inspect.getsource(paint):
                continue
            if not any("enterEvent" in k.__dict__ for k in cls.__mro__ if k.__module__.startswith("jeeves")):
                missing.append(cls.__name__)
    assert not missing
