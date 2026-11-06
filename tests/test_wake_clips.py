"""Recent wake-ups: kept as clips, marked good or bad, and learned from."""
import time


def _wait(pred, t=5):
    end = time.time() + t
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class STT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm, prompt="", language="en"):
        return self.text


def _wake(engine, monkeypatch, heard):
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT(heard))
    engine._wake_words["microphone"] = [("jeeves", 1.0, 0.12)]
    engine._start_listen("microphone", "jeeves", "jeeves", [], None, 0, name_audio=[b"\1\0" * 480], conf=1.0)
    s = engine.sessions["microphone"]
    s.feed(b"\1\0" * 4800, True, time.time())
    s.got_speech, s.voiced_frames = True, 50
    engine.end_session(s)
    return s


def test_a_wake_up_is_kept_with_what_happened(engine, monkeypatch):
    s = _wake(engine, monkeypatch, "Guh.")
    assert _wait(lambda: engine.clips.list() and engine.clips.list()[0]["outcome"].startswith("ignored"))
    c = engine.clips.list()[0]
    assert c["heard"] == "Guh." and c["agent"] == "jeeves" and c["words"] == [["jeeves", 1.0, 0.12]]
    assert _wait(lambda: engine.clips.list()[0]["path"])
    assert s.clip_id == c["id"]


def test_marking_teaches_decoys_recordings_and_length(engine, monkeypatch):
    for _ in range(3):
        _wake(engine, monkeypatch, "Guh.")
    assert _wait(lambda: len(engine.clips.list()) == 3 and all(c["path"] for c in engine.clips.list()))
    for c in engine.clips.list():
        res = engine.clips.mark(c["id"], False, "guh")
    assert engine.settings.get("wake_word.extra_decoys") == ["guh"]
    for _ in range(3):
        monkeypatch.setattr(engine, "handle_text", lambda *a, **k: {})
        engine._wake_words["microphone"] = [("jeeves", 1.0, 0.42)]
        engine._start_listen("microphone", "jeeves", "jeeves", [], None, 0, name_audio=[b"\1\0" * 480], conf=1.0)
        s = engine.sessions["microphone"]
        s.feed(b"\1\0" * 4800, True, time.time())
        s.got_speech, s.voiced_frames = True, 50
        monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("Jeeves, what time is it"))
        engine.end_session(s)
    assert _wait(lambda: len(engine.clips.list()) == 6 and all(c["path"] for c in engine.clips.list()))
    for c in [c for c in engine.clips.list() if not c["verdict"]]:
        res = engine.clips.mark(c["id"], True)
    assert res["recording"] == "Jeeves, what time is it"
    assert 0.12 < engine.settings.get("wake_word.learned_min_seconds") < 0.42
    assert len(engine.training.recordings()) == 6         # the "guh"s too, with what they really were


def test_only_the_last_ones_are_kept(engine, monkeypatch):
    engine.settings.set("wake_word.clips_kept", 2)
    for _ in range(3):
        engine.clips.add(b"\0\0" * 1600, "jeeves", "microphone")
    assert len(engine.clips.list()) == 2
