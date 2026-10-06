"""Screen coordinates, Control Mode mouse mapping and per-agent memory."""
import pytest


def test_mapper_scales_hidpi_and_offsets():
    from jeeves.functions.partials.screen import Mapper
    # two 1920x1080 monitors at scale 2 (screenshot 7680x2160 physical), desktop starts at -1920
    mp = Mapper((7680, 2160), (-1920, 0, 3840, 1080))
    assert mp.point(0, 0) == (-1920, 0)
    assert mp.point(3840, 1080) == (0, 540)
    assert mp.rect_to_image((0, 540, 960, 270)) == (3840, 1080, 1920, 540)


def test_mapper_without_layout_is_one_to_one(monkeypatch):
    from jeeves.daemon import desktop as dk
    from jeeves.functions.partials.screen import Mapper
    monkeypatch.setattr(dk, "outputs", lambda: [dk.Output("default", 0, 0, 1920, 1080)])
    assert Mapper((2560, 720)).point(100, 50) == (100, 50)


def test_absolute_device_covers_the_whole_desktop():
    from jeeves.daemon.control import ABS_MAX, abs_value
    assert abs_value(-1920, -1920, 3840) == 0
    assert abs_value(1919, -1920, 3840) == ABS_MAX
    assert abs_value(0, -1920, 3840) == round(1920 * ABS_MAX / 3839)
    assert abs_value(99999, 0, 1920) == ABS_MAX and abs_value(-5, 0, 1920) == 0


def test_screenshot_order_per_desktop(monkeypatch, tmp_path):
    from jeeves.functions.partials import screen
    monkeypatch.setattr(screen, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(screen, "desktop", lambda: "kde")
    assert [t for t, _ in screen.screenshot_commands(tmp_path / "s.png")] == ["spectacle", "portal", "grim"]
    monkeypatch.setattr(screen, "desktop", lambda: "hyprland")
    assert [t for t, _ in screen.screenshot_commands(tmp_path / "s.png")][:2] == ["grim", "portal"]


def test_screenshot_falls_back_and_reports_every_failure(monkeypatch, tmp_path):
    import subprocess
    from jeeves.functions.base import FunctionError
    from jeeves.functions.partials import screen
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "grim":
            open(cmd[-1], "wb").write(b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (10).to_bytes(4, "big") + (5).to_bytes(4, "big"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "not allowed to take screenshots")
    monkeypatch.setattr(screen.subprocess, "run", fake_run)
    monkeypatch.setattr(screen, "graphical_env", dict)
    monkeypatch.setattr(screen, "screenshot_commands", lambda p: [("spectacle", ["spectacle", str(p)]),
                                                                   ("grim", ["grim", str(p)])])
    shot = screen.screenshot()
    assert calls == ["spectacle", "grim"] and screen.last_tool == "grim" and screen._image_size(shot) == (10, 5)
    shot.unlink()
    monkeypatch.setattr(screen, "screenshot_commands", lambda p: [("spectacle", ["spectacle", str(p)])])
    with pytest.raises(FunctionError, match="spectacle: not allowed"):
        screen.screenshot()


def test_agent_memory_defaults_and_off(engine):
    from jeeves.config import agent_memory
    engine.settings.set("memory.recent_count", 4)
    assert agent_memory({}, engine.settings) == {"enabled": True, "recent": 4, "notes": 30, "own_only": False}
    assert agent_memory({"memory": {"recent": 1, "notes": 2}}, engine.settings)["recent"] == 1
    off = agent_memory({"memory": {"enabled": False, "recent": 9}}, engine.settings)
    assert off["recent"] == 0 and off["notes"] == 0


def test_memory_limits_and_own_only(engine):
    engine.memory.add("jeeves note", agent="jeeves")
    engine.memory.add("other note", agent="friday")
    engine.memory.add("latest", agent="friday")
    assert "jeeves note" not in engine.memory.context_for("jeeves", limit=2)
    own = engine.memory.context_for("jeeves", limit=10, own_only=True)
    assert "jeeves note" in own and "other note" not in own
    assert engine.memory.context_for("jeeves", limit=0) == ""


def test_remember_refused_when_memory_off(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.base import FunctionError
    agent = dict(engine.agents()["jeeves"], memory={"enabled": False})
    ctx = FunctionContext(engine, "jeeves", agent, new_entry("x", "jeeves", "test"))
    with pytest.raises(FunctionError, match="memory turned off"):
        ctx.call("remember", text="my locker is 42")
    assert engine.memory.all() == []
    on = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "test"))
    on.call("remember", text="my locker is 42")
    assert engine.memory.all()[0]["text"] == "my locker is 42"


@pytest.mark.parametrize("text,function", [
    ("Jeeves, what's on my screen?", "screen_reading"),
    ("Jeeves, what does this error say", "screen_reading"),
    ("Jeeves, read what it says at the top", "screen_reading"),
    ("Jeeves, can you see my screen", "screen_reading"),
    ("Jeeves, click the play button", "control_mode"),
    ("Jeeves, could you type hello world please", "control_mode"),
    ("Jeeves, press ctrl+s", "control_mode"),
    ("Jeeves, at 7pm open OBS", "timers"),
    ("Jeeves, what is the capital of France", "local_response"),
])
def test_unmistakable_requests_route_without_a_model(engine, text, function):
    engine.settings.set("agents.jeeves.functions.control_mode", True)
    agent = dict(engine.agents()["jeeves"], id="jeeves")
    assert engine.intent.decide(agent, text).function == function


def test_disabled_control_mode_says_so(engine):
    entry = engine.dry_run("Jeeves, click Save", "jeeves")
    assert entry["status"] == "refused" and "Control Mode is turned off" in entry["response"]


def test_read_at_the_top_is_not_a_timer(engine):
    agent = dict(engine.agents()["jeeves"], id="jeeves")
    d = engine.intent.decide(agent, "Jeeves, read what it says at the top")
    assert d.function == "screen_reading" and d.args["region"] == "top"


class _Ctx:
    def __init__(self, hits):
        self.hits, self.calls = hits, []
        self.engine = type("E", (), {"control": type("C", (), {"desktop_box": lambda s: (0, 0, 1920, 1080)})()})()

    def call(self, name, **kw):
        from jeeves.functions.base import FunctionError
        self.calls.append(kw["target"])
        if kw["target"] in self.hits:
            return self.hits[kw["target"]]
        raise FunctionError("not found")


def test_control_phrases():
    from jeeves.functions.control_phrases import simple_actions
    ctx = _Ctx({"play": {"x": 500, "y": 300, "text": "Play"}})
    acts = simple_actions(ctx, "double-click the play button")
    assert ctx.calls == ["the play button", "play"]
    assert acts[0] == {"do": "move", "x": 500, "y": 300, "absolute": True, "duration": 0.15}
    assert [a["button"] for a in acts if a["do"] == "button"] == ["BTN_LEFT", "BTN_LEFT"]
    assert simple_actions(ctx, "type 'Hello, there'") == [{"do": "type", "text": "Hello, there"}]
    assert simple_actions(ctx, "press ctrl+shift+t") == [
        {"do": "key", "key": "ctrl", "state": "down"}, {"do": "key", "key": "shift", "state": "down"},
        {"do": "key", "key": "t", "state": "tap"}, {"do": "key", "key": "shift", "state": "up"},
        {"do": "key", "key": "ctrl", "state": "up"}]
    assert simple_actions(ctx, "press the button that opens settings") is None     # the model plans that
    assert simple_actions(ctx, "scroll down a lot") == [{"do": "scroll", "amount": -10}]
    assert simple_actions(ctx, "right-click here") == [{"do": "button", "button": "BTN_RIGHT", "state": "tap"}]
    assert simple_actions(ctx, "move the mouse to the top left corner")[0]["x"] < 100
    assert simple_actions(ctx, "let go of everything") == [{"do": "release_all"}]
    from jeeves.functions.base import FunctionError
    with pytest.raises(FunctionError, match="can't see"):
        simple_actions(ctx, "click Export")


def _two_monitors(monkeypatch):
    from jeeves.daemon import desktop as dk
    outs = [dk.Output("DP-2", 1920, 0, 1280, 720, 2.0, False), dk.Output("DP-1", 0, 0, 1920, 1080, 1.0, True)]
    monkeypatch.setattr(dk, "outputs", lambda: outs)
    monkeypatch.setattr(dk, "focused", lambda: dk.Window("1", "kate", "notes", 2000, 100, 400, 300))
    return outs


def test_pick_monitors(monkeypatch):
    from jeeves.functions.partials.screen import pick_outputs
    _two_monitors(monkeypatch)
    names = lambda s: [o.name for o in pick_outputs(s)]  # noqa: E731
    assert names("all") == [] and names("") == []
    assert names("left") == ["DP-1"] and names("right") == ["DP-2"]
    assert names("primary") == ["DP-1"] and names("second") == ["DP-2"]
    assert names("current") == ["DP-2"]            # the focused window is on DP-2
    assert names("other") == ["DP-1"] and names("dp-2") == ["DP-2"]


def test_regions_are_per_monitor(monkeypatch):
    from jeeves.functions.partials.screen import Mapper, _rect_for
    _two_monitors(monkeypatch)
    mp = Mapper((6400, 2160), (0, 0, 3200, 1080))   # whole desktop shot at 2x
    assert _rect_for("anywhere", 6400, 2160, mp, "right") == (3840, 0, 2560, 1440)
    x, y, w, h = _rect_for("top", 6400, 2160, mp, "right")
    assert (x, y) == (3840, 0) and w == 2560 and h == 1440 // 3
    assert _rect_for("top", 6400, 2160, mp, "all") == (0, 0, 6400, 2160 // 3)


def test_screen_words_pick_monitor_and_region():
    from jeeves.daemon.intent import guess_region, guess_screen
    assert (guess_region("read the top of my left screen"), guess_screen("read the top of my left screen")) == \
        ("top", "left")
    assert guess_region("what's on the right monitor") == "anywhere"
    assert guess_screen("what's on all my screens") == "all"
