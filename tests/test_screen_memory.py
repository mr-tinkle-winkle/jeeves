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
