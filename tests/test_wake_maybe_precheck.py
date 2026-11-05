"""The indicator shows the moment a name might have been heard (and spins away red if it wasn't one);
Whisper only checks for a missed name when the small wake model heard something like one."""
import time


def _events(engine):
    out = []
    engine.publish = lambda t, d: out.append((t, d))
    return out


def test_a_maybe_shows_at_once_and_a_real_call_takes_over(engine, monkeypatch):
    ev = _events(engine)
    monkeypatch.setattr(engine, "agent_by_name", lambda name, source=None: "jeeves")
    engine.wake_maybe("microphone", "jeeves")
    first = [d for t, d in ev if t == "indicator"][-1]
    assert first["stage"] == "listening" and first["agent"] == "jeeves" and first["request"] == "maybe-microphone"
    engine.clear_maybe("microphone")                      # (what _start_listen does when the call is real)
    assert [d for t, d in ev if t == "indicator"][-1]["stage"] == "idle"


def test_a_misheard_name_spins_away_red(engine, monkeypatch):
    ev = _events(engine)
    monkeypatch.setattr(engine, "agent_by_name", lambda name, source=None: "jeeves")
    monkeypatch.setattr(engine, "MAYBE_SECONDS", 0.2)
    engine.wake_maybe("microphone", "jeeves")
    time.sleep(0.5)
    stages = [d["stage"] for t, d in ev if t == "indicator"]
    assert "misheard" in stages
    time.sleep(1.1)
    assert [d["stage"] for t, d in ev if t == "indicator"][-1] == "idle"


def test_a_low_confidence_name_that_is_ignored_shows_misheard(engine, monkeypatch):
    ev = _events(engine)
    monkeypatch.setattr(engine, "agent_by_name", lambda name, source=None: "jeeves")
    engine.wake_maybe("microphone", "jeeves")
    engine.settings.set("wake_word.verify_near_misses", False)
    engine.on_wake("microphone", "jeeves", 0.05, [])
    assert "misheard" in [d["stage"] for t, d in ev if t == "indicator"]


def test_whisper_only_checks_what_sounds_like_a_name(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(engine, "transcribe", lambda pcm, agent=None: calls.append(1) or "")

    class Spot:
        def __init__(self, text):
            self.text = text

        def quick_text(self, pcm):
            return self.text
    monkeypatch.setattr(engine.models, "wake_spotter", lambda: Spot("the game tonight was crazy"))
    engine.on_backup_utterance("microphone", b"\0" * 32000)
    assert not calls                                             # nothing like "Jeeves": no Whisper
    monkeypatch.setattr(engine.models, "wake_spotter", lambda: Spot("hey jeez what time is it"))
    engine.on_backup_utterance("microphone", b"\0" * 32000)
    assert calls                                                 # close enough: Whisper checks
    calls.clear()
    engine.on_backup_utterance("desktop", b"\0" * 32000)
    assert not calls                                             # desktop audio: the wake model alone
    engine.settings.set("wake_word.backup_precheck", False)
    engine.on_backup_utterance("desktop", b"\0" * 32000)
    assert calls                                                 # the check off: as before
