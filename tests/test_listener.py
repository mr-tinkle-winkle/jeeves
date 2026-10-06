import array
import time

from jeeves.daemon.audio import FRAME_SAMPLES, rms, to_wav, wav_to_pcm
from jeeves.daemon.listener import Listener, Segmenter, Session
from jeeves.models.manager import ModelUnavailable


def frame(level: float) -> bytes:
    v = int(level * 32767)
    return array.array("h", [v, -v] * (FRAME_SAMPLES // 2)).tobytes()


LOUD, QUIET = frame(0.2), frame(0.0)


def test_rms_and_wav_roundtrip():
    assert rms(QUIET) == 0.0 and 0.19 < rms(LOUD) < 0.21
    pcm, rate = wav_to_pcm(to_wav(LOUD * 3))
    assert pcm == LOUD * 3 and rate == 16000


def test_session_end_rules():
    s = Session("microphone", "jeeves", "request")
    t0 = s.started
    s.feed(LOUD, True, t0 + 0.1)
    assert not s.should_end(t0 + 0.5, eos=1.0)
    assert s.should_end(t0 + 1.2, eos=1.0)                 # silence after speech
    s.extend_until = t0 + 6                                 # mic clicked: +5 s
    assert not s.should_end(t0 + 3, eos=1.0)
    s.hold = True                                           # mic held
    assert not s.should_end(t0 + 30, eos=1.0)
    s.hold = False
    assert s.should_end(t0 + 30, eos=1.0)
    empty = Session("microphone", "jeeves", "request")
    assert not empty.should_end(empty.started + 3, eos=1.0)
    assert empty.should_end(empty.started + 7, eos=1.0)     # nobody spoke


def test_segmenter_splits_utterances():
    seg = Segmenter()
    now, out = 0.0, []
    for f in [QUIET] * 5 + [LOUD] * 20 + [QUIET] * 50 + [LOUD] * 10 + [QUIET] * 50:
        now += 0.03
        got = seg.feed(f, rms(f) > 0.01, now, eos=0.6)
        if got:
            out.append(got)
    assert len(out) == 2


class FakeSTT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm, prompt="", language="en"):
        return self.text


def test_wake_then_request_flow(engine, monkeypatch):
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: FakeSTT("set a timer for 7 minutes"))
    engine.models.wake_spotter = lambda: None
    engine.on_wake("microphone", "jeeves", 0.9, [LOUD] * 20)          # the name's tail, then the request
    s = engine.sessions["microphone"]
    assert s.agent_id == "jeeves" and s.got_speech
    engine.end_session(s)
    deadline = time.time() + 5
    while time.time() < deadline and not engine.timers.list():
        time.sleep(0.02)
    assert engine.timers.list()[0]["total"] == 420


def test_wake_below_threshold_is_ignored(engine):
    engine.on_wake("microphone", "jeeves", 0.3, [])
    assert "microphone" not in engine.sessions


def test_stt_unavailable_queues_voice_request(engine, monkeypatch):
    def unavailable(agent=None):
        raise ModelUnavailable("stt", "unloaded while steam is open", queueable=True)
    monkeypatch.setattr(engine.models, "stt", unavailable)
    s = engine.open_session("microphone", "jeeves", "request")
    s.feed(LOUD, True, time.time())
    engine._finish_session(s)
    assert engine.queue and engine.queue[0][0] == "voice"
    assert any(i["stage"] == "unavailable" for i in engine.indicators.values())
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: FakeSTT("set a timer for 8 minutes"))
    engine._model_back("stt")
    deadline = time.time() + 5
    while time.time() < deadline and not engine.timers.list():
        time.sleep(0.02)
    assert engine.timers.list()[0]["total"] == 480


def test_transcript_wake_and_summary(engine, monkeypatch):
    engine.settings.set("summary.enabled", True)
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: FakeSTT("Jeeves, set a timer for 3 minutes"))
    engine.on_utterance("microphone", LOUD * 5)
    assert "Jeeves, set a timer" in engine.summary.text(5)
    deadline = time.time() + 5
    while time.time() < deadline and not engine.timers.list():
        time.sleep(0.02)
    assert engine.timers.list()


def test_answer_session_routes_to_waiting_question(engine, monkeypatch):
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: FakeSTT("set a timer for 2 minutes"))
    engine.settings.set("agents.jeeves.functions", {"local_response": False})
    res = engine.handle_text("Jeeves, the noodle thing")
    deadline = time.time() + 5
    while time.time() < deadline and not engine.answer_pending():
        time.sleep(0.02)
    lst = Listener(engine, "microphone")
    for _ in range(5):
        lst.process(LOUD)        # speech opens an answer session without a wake word
    s = engine.sessions["microphone"]
    assert s.mode == "answer"
    engine.end_session(s)
    while time.time() < deadline and (engine.history.get(res["id"]) or {}).get("status") != "done":
        time.sleep(0.02)
    assert engine.history.get(res["id"])["function"] == "timers"
