"""The Main agent, Lobotomize, rudimentary Run commands and mute, timer ticking, speech checks, the
overlay's start-up, and what thoughts show."""
import sys
import time


def _said(engine):
    out = []
    engine.speak = lambda ctx, t: out.append(t)
    return out


def test_a_manual_request_naming_nobody_goes_to_the_main_agent(engine):
    assert "error" in engine.handle_text("set a timer for 2 minutes")              # no Main agent yet
    engine.settings.set("general.main_agent", "jeeves")
    res = engine.handle_text("set a timer for 2 minutes", wait=True)
    assert res["agent"] == "jeeves" and engine.history.get(res["id"])["function"] == "timers"
    assert engine.handle_text("set a timer for 3 minutes", source="voice:microphone").get("error")   # wake words: no
    engine.settings.set("agents.jeeves.enabled", False)                               # an agent that's off: no
    assert "error" in engine.handle_text("set a timer for 4 minutes")


def test_lobotomize_forgets_everything_the_agent_remembers(engine):
    _said(engine)
    engine.memory.add("the user likes tea", permanent=True, agent="jeeves")
    engine.memory.add("someone else's note", permanent=True, agent="other")
    engine.handle_text("Jeeves, set a timer for 2 minutes", wait=True)
    assert engine.history.list(agent="jeeves")
    res = engine.lobotomize("jeeves")
    assert res["notes"] == 1 and res["requests"] >= 1
    assert not engine.history.list(agent="jeeves")
    assert [n["agent"] for n in engine.memory.all()] == ["other"]


def test_rudimentary_run_commands_and_mute(engine):
    from jeeves.functions.rudimentary import NAME, tidy_spoken
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": True, "function": NAME, "confirm": "never"})
    assert [f.name for f in engine.registry.enabled_for(engine.agents()["jeeves"])] == [NAME]
    said = _said(engine)
    res = engine.history.get(engine.handle_text(f"Jeeves, {sys.executable} -c \"print('hi there')\"", wait=True)["id"])
    assert res["function"] == NAME and said[-1] == "hi there"
    assert tidy_spoken("systemctl dash dash user restart pipewire") == ["systemctl", "--user", "restart", "pipewire"]
    # muted: nothing is synthesized or played, the text is still there
    from jeeves.daemon.engine import Engine
    engine.speak = Engine.speak.__get__(engine)
    engine._synth = lambda *a: (_ for _ in ()).throw(AssertionError("muted agents don't speak"))
    engine.settings.set("agents.jeeves.rudimentary", {"enabled": True, "function": NAME, "mute": True})
    res = engine.history.get(engine.handle_text(f"Jeeves, {sys.executable} -c \"print('quiet')\"", wait=True)["id"])
    assert res["status"] == "done" and res["response"] == "quiet"
    assert any(t["kind"] == "muted" for t in res["trace"])


def test_timers_tick_only_near_the_end_by_default(engine):
    t = engine.timers
    tid = t.add(300, "long")
    now = time.time()
    assert t.ticking_for(now) is None                                       # ticking is off by default
    engine.settings.set("timers.tick", True)
    assert t.ticking_for(now) is None                                       # 5 minutes left: not yet
    assert t.ticking_for(now + 250)["id"] == tid                            # the last minute: ticks
    engine.settings.set("timers.tick_only_under", False)
    assert t.ticking_for(now)["id"] == tid                                  # the whole time
    t.cancel()
    from jeeves.daemon.timers import tick_pcm
    pcm = tick_pcm(16000)
    assert len(pcm) == 32000 and max(pcm[:2000]) > 0 and not any(pcm[16000:])


def test_silent_speech_is_said_with_espeak_instead(engine, monkeypatch):
    from jeeves.daemon import engine as eng_mod
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    played = []

    class FakePlayback:
        def __init__(self, pcm, rate, targets):
            self.pcm, self.rate, self.targets, self.error = pcm, rate, targets, None
            import threading
            self.stopped = threading.Event()

        def peak(self):
            return 0 if not any(self.pcm) else 9000

        def seconds(self):
            return len(self.pcm) / 2 / self.rate

        def play(self):
            played.append(self.pcm)
    monkeypatch.setattr(eng_mod, "Playback", FakePlayback)
    engine._synth = lambda agent, text: (b"\0" * 32000, 16000)
    import jeeves.models.backends as be
    monkeypatch.setattr(be.EspeakTTS, "synth", lambda self, text, v, st=None: (b"\x10\x20" * 8000, 16000))
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    engine.speak(ctx, "Certainly, sir.")
    assert played and any(played[-1])
    kinds = [t["kind"] for t in ctx.entry["trace"]]
    assert "speech_silent" in kinds and "speech" in kinds


def test_the_overlay_child_can_import_jeeves_from_anywhere(engine):
    import os
    import subprocess
    env = engine._overlay_env({"PATH": os.environ.get("PATH", "")})
    out = subprocess.run([sys.executable, "-c", "import jeeves.overlay"], env=env, cwd="/", capture_output=True)
    assert out.returncode == 0, out.stderr


def test_thoughts_show_what_was_asked(engine):
    _said(engine)
    res = engine.handle_text("Jeeves, set a timer for 2 minutes", wait=True)
    th = engine.thoughts(res["id"])
    assert th["text"] == "set a timer for 2 minutes" and th["source"] == "text"
