"""Waking up: the speech detector, the name's own tail, the wake-up reply ("Yes?"), and telling a
real call from a false wake ("geez", "believes") with the transcript."""
import array
import time

import pytest

from jeeves.daemon import vad
from jeeves.daemon.audio import FRAME_SAMPLES
from jeeves.daemon.listener import HOLD_LIMIT, LEVEL_RUN, Listener, Session

FRAME = FRAME_SAMPLES * 2


def frame(level: float) -> bytes:
    v = int(level * 32767)
    return array.array("h", [v, -v] * (FRAME_SAMPLES // 2)).tobytes()


LOUD, QUIET = frame(0.2), frame(0.0)


class FakeSTT:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def transcribe(self, pcm, prompt="", language="en"):
        self.calls += 1
        self.prompt = prompt
        return self.text


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not cond():
        time.sleep(0.02)
    return cond()


def stages(engine):
    seen = []
    orig = engine.set_indicator

    def record(request_id, agent_id, stage, detail="", **extra):
        seen.append(stage)
        return orig(request_id, agent_id, stage, detail, **extra)
    engine.set_indicator = record
    return seen


# ---------------------------------------------------------------------------- the speech detector
@pytest.mark.skipif(not vad.available(), reason="onnxruntime isn't installed")
def test_speech_detector_hears_no_speech_in_silence_or_noise():
    import random
    det = vad.SpeechDetector.create()
    rnd = random.Random(1)
    noise = [array.array("h", [int(rnd.gauss(0, 300)) for _ in range(FRAME_SAMPLES)]).tobytes()
             for _ in range(100)]
    assert not any(det.feed(QUIET) for _ in range(60))
    assert not any(det.feed(f) for f in noise)
    assert 0.0 <= det.prob <= 1.0
    det.feed(QUIET[:301])                       # an odd, short read doesn't break it


def test_level_meter_when_there_is_no_speech_detector(engine, monkeypatch):
    monkeypatch.setattr(vad.SpeechDetector, "create", classmethod(lambda cls: None))
    engine.settings.set("audio.speech_detector", "auto")
    lst = Listener(engine, "microphone")
    lst.process(QUIET)
    assert lst.detector is None
    # a key press or a mouse click is a frame or two: not speech
    lst.process(LOUD)
    lst.process(QUIET)
    assert not any(lst.voiced_ring)
    for _ in range(LEVEL_RUN):
        lst.process(LOUD)
    assert lst.voiced_ring[-1]


# ---------------------------------------------------------------------------- the name's own tail
def test_the_name_still_sounding_is_not_the_request():
    s = Session("microphone", "jeeves", "request")
    t0 = s.started
    s.tail_until = t0 + 0.6
    for i in range(5):                           # the detector still hears the name for a moment
        s.feed(QUIET, True, t0 + 0.03 * i, sure=False)
    s.feed(QUIET, False, t0 + 0.2)
    assert not s.got_speech and s.voiced_frames == 0 and not s.tail_until
    s.feed(LOUD, True, t0 + 1.5)                 # then you talk
    assert s.got_speech and s.first_voice == t0 + 1.5


def test_speech_running_on_from_the_name_is_the_request():
    s = Session("microphone", "jeeves", "request")
    t0 = s.started
    s.tail_until = t0 + 0.6
    s.feed(QUIET, True, t0, sure=False)          # the name dying away...
    for i in range(1, 15):                       # ...into "set a timer", said fast in one breath
        s.feed(LOUD, True, t0 + 0.03 * i, sure=True)
    assert s.got_speech and s.voiced_frames == 14 and s.first_voice == pytest.approx(t0 + 0.03)
    t = Session("microphone", "jeeves", "request")
    t.tail_until = t.started + 0.6
    for i in range(30):                          # speech the detector is sure of, past the name's end
        t.feed(QUIET, True, t.started + 0.03 * i, sure=False)
    assert t.got_speech and t.voiced_frames == 10


def test_a_held_mic_still_ends_eventually():
    s = Session("microphone", "jeeves", "request", max_seconds=30)
    s.hold = True
    assert not s.should_end(s.started + 120, eos=1.2)
    assert s.should_end(s.started + HOLD_LIMIT + 1, eos=1.2)


# ---------------------------------------------------------------------------- the wake-up reply
def test_wake_reply_after_a_quiet_moment(engine):
    engine.settings.set("agents.jeeves.wake_reply", "Yes? | Hello.")
    lst = Listener(engine, "microphone")
    engine.listeners["microphone"] = lst
    engine.on_wake("microphone", "jeeves", 0.99, [QUIET] * 5, words=0, after_voiced=[False] * 5)
    s = engine.sessions["microphone"]
    assert s.reply_at and not s.replied            # not straight away: you may be about to go on
    s.reply_at = time.time() - 0.01
    lst.process(QUIET)
    assert s.replied and not s.reply_at
    assert engine._wake_reply_phrases(engine.agents()["jeeves"]) == ["Yes?", "Hello."]


def test_no_wake_reply_when_you_go_on_talking(engine):
    engine.settings.set("agents.jeeves.wake_reply", "Yes?")
    lst = Listener(engine, "microphone")
    engine.listeners["microphone"] = lst
    engine.on_wake("microphone", "jeeves", 0.99, [QUIET] * 5, words=0, after_voiced=[False] * 5)
    s = engine.sessions["microphone"]
    for _ in range(LEVEL_RUN + 2):
        lst.process(LOUD)                          # the request starts before the reply was due
    s.reply_at = time.time() - 0.01
    lst.process(LOUD)
    assert not s.replied and not s.reply_at


def test_no_wake_reply_unless_the_agent_has_one(engine):
    engine.on_wake("microphone", "jeeves", 0.99, [QUIET] * 5, words=0, after_voiced=[False] * 5)
    assert not engine.sessions["microphone"].reply_at


def test_going_straight_on_with_the_request_means_no_reply(engine):
    engine.settings.set("agents.jeeves.wake_reply", "Yes?")
    lst = Listener(engine, "microphone")
    engine.listeners["microphone"] = lst
    engine.on_wake("microphone", "jeeves", 0.99, [LOUD] * 20, words=2, after_voiced=[True] * 20)
    s = engine.sessions["microphone"]
    for _ in range(30):                       # "Jeeves open the mixer": speech runs on from the name
        lst.process(LOUD)
    s.reply_at = s.reply_at and time.time() - 0.01
    lst.process(LOUD)
    assert s.got_speech and not s.replied


def test_a_breath_after_the_name_still_gets_a_reply(engine):
    # the wake model hearing "words" after the name (a breath, room noise) used to cancel the reply
    engine.settings.set("agents.jeeves.wake_reply", "Yes?")
    lst = Listener(engine, "microphone")
    engine.listeners["microphone"] = lst
    engine.on_wake("microphone", "jeeves", 0.99, [QUIET] * 5, words=1, after_voiced=[False] * 5)
    s = engine.sessions["microphone"]
    s.reply_at = time.time() - 0.01
    lst.process(QUIET)
    assert s.replied


def test_name_still_sounding_when_the_listen_opens(engine):
    engine.on_wake("microphone", "jeeves", 0.99, [LOUD] * 6, words=0, after_voiced=[True] * 6)
    s = engine.sessions["microphone"]
    assert not s.got_speech and s.tail_until > time.time()


# ---------------------------------------------------------------------------- trusting the wake
def test_trusted_wakes():
    s = Session("microphone", "jeeves", "request")
    s.wake_conf = 1.0
    s.first_voice = s.started + 1.0            # a clear name, a pause, then the request
    assert s.trusted()
    s.first_voice = s.started + 0.1            # the name inside running speech
    assert not s.trusted()
    s.replied = True                           # it said "Yes?" and you answered
    assert s.trusted()
    s.wake_conf = 0.7
    assert not s.trusted()


def test_name_matching_in_the_transcript(engine):
    assert engine.strip_name("Okay so, Jeeves, set a timer", "jeeves") == (True, "set a timer")
    assert engine.strip_name("Jeevs open the mixer", "jeeves") == (True, "open the mixer")
    assert engine.strip_name("Geeves, what time is it", "jeeves")[0]
    for heard in ("geez that was loud", "Keanu Reeves is great", "pass me the cheese please",
                  "she believes it gives you jeans", "Christmas eves are fun"):
        assert not engine.strip_name(heard, "jeeves")[0], heard


def _wake_session(engine, text, gap):
    stt = FakeSTT(text)
    engine.models.stt = lambda agent=None: stt
    handled = []
    engine.handle_text = lambda t, agent_id=None, **k: handled.append(t)
    engine.on_wake("microphone", "jeeves", 1.0, [], name_audio=[LOUD] * 15)
    s = engine.sessions["microphone"]
    for i in range(20):
        s.feed(LOUD, True, s.started + gap + 0.03 * i)
    return s, stt, handled


def test_false_wake_is_dropped_quietly(engine):
    seen = stages(engine)
    s, stt, handled = _wake_session(engine, "geez that was loud", gap=0.1)
    engine.end_session(s)
    assert wait_for(lambda: stt.calls == 1)
    time.sleep(0.2)
    assert handled == [] and "unclear" not in seen


def test_the_name_is_taken_off_the_request(engine):
    s, stt, handled = _wake_session(engine, "Jeeves, set a timer for 7 minutes", gap=0.1)
    engine.end_session(s)
    assert wait_for(lambda: handled)
    assert handled == ["set a timer for 7 minutes"]


def test_a_clear_call_counts_even_when_the_name_is_misheard(engine):
    s, stt, handled = _wake_session(engine, "Cheese, set a timer for 7 minutes", gap=1.0)
    engine.end_session(s)
    assert wait_for(lambda: handled)
    assert handled == ["Cheese, set a timer for 7 minutes"]


def test_too_little_speech_after_a_doubtful_wake_is_silent(engine):
    seen = stages(engine)
    engine.on_wake("microphone", "jeeves", 1.0, [], name_audio=[LOUD] * 15)
    s = engine.sessions["microphone"]
    s.feed(LOUD, True, s.started + 0.1)        # a blip right after "geez"
    engine.end_session(s)
    assert "unclear" not in seen


def test_the_name_while_it_waits_for_your_answer_opens_the_answer(engine, monkeypatch):
    monkeypatch.setattr(engine, "answer_pending", lambda source=None: "jeeves")
    engine.on_wake("microphone", "jeeves", 0.99, [LOUD] * 10)
    assert engine.sessions["microphone"].mode == "answer"


# ---------------------------------------------------------------------------- what the models are told
def test_speech_recognition_is_told_the_names(engine):
    engine.settings.set("agents.deskbot", {**engine.agents()["jeeves"], "name": "Deskbot",
                                           "call_names": ["Deskbot", "Desk"]})
    prompt = engine.stt_prompt()
    assert prompt.startswith("Desk, Deskbot, Jeeves.")


def test_wake_grammar_has_every_enabled_agent_on_the_source(engine):
    engine.settings.set("agents.deskbot", {**engine.agents()["jeeves"], "name": "Deskbot",
                                           "call_names": ["Deskbot"], "enable_when_open": ["obs"]})
    engine.settings.set("agents.quiet", {**engine.agents()["jeeves"], "name": "Quiet", "call_names": ["Quiet"],
                                         "enabled": False})
    assert engine.wake_names("microphone") == ["deskbot", "jeeves"]


def test_a_busy_computer_doesnt_cut_the_request_short(engine, monkeypatch):
    """The listener falls behind (a game loading) and then works through the queued audio: that's no
    silence, so the listen goes on."""
    from jeeves.daemon import listener as listener_mod
    clock = {"t": 1000.0}
    monkeypatch.setattr(listener_mod.time, "time", lambda: clock["t"])
    lst = Listener(engine, "microphone")
    engine.listeners["microphone"] = lst
    engine.on_wake("microphone", "jeeves", 0.99, [])
    s = engine.sessions["microphone"]
    s.started = clock["t"]
    for f in [LOUD] * 10 + [QUIET] * 10:      # you start talking, pause for breath...
        clock["t"] += 0.03
        lst.process(f, backlog=0)
    assert s.got_speech
    clock["t"] += 2.0                         # ...the computer stalls for two seconds...
    queued = [QUIET] * 10 + [LOUD] * 30       # ...while you went on: the listener works through it after
    for i, f in enumerate(queued):
        lst.process(f, backlog=len(queued) - i)
    assert engine.sessions.get("microphone") is s and not s.ended


# ---------------------------------------------------------------------------- calls the wake model misses
def _utterance(lst, frames, hit_at=None):
    for i, f in enumerate(frames):
        lst._backup(f, f is LOUD and i >= 2, time.time() + 0.03 * i, 0.6, i == hit_at, None)


def test_speech_recognition_checks_what_the_wake_model_heard_no_name_in(engine, monkeypatch):
    seen = []
    monkeypatch.setattr(engine, "run_async", lambda fn, *a: seen.append(fn.__name__))
    lst = Listener(engine, "microphone")
    _utterance(lst, [LOUD] * 30 + [QUIET] * 40)
    assert seen == ["on_backup_utterance"]
    seen.clear()
    _utterance(lst, [LOUD] * 30 + [QUIET] * 40, hit_at=10)     # the wake model got this one
    assert seen == []
    _utterance(lst, [LOUD] * 400 + [QUIET] * 40)               # a long talk with someone else
    assert seen == []


def test_a_missed_call_is_answered(engine):
    handled = []
    engine.handle_text = lambda text, agent_id=None, **k: handled.append((text, agent_id))
    engine.models.stt = lambda agent=None: FakeSTT("Hey Jeeves, what time is it?")
    engine.on_backup_utterance("microphone", LOUD * 30)
    assert handled == [("what time is it?", "jeeves")]
    engine.models.stt = lambda agent=None: FakeSTT("geez that was loud")
    engine.on_backup_utterance("microphone", LOUD * 30)
    assert len(handled) == 1
    engine.models.stt = lambda agent=None: FakeSTT("I told my friend about Jeeves yesterday")
    engine.on_backup_utterance("microphone", LOUD * 30)          # a mention, not a call
    assert len(handled) == 1


def test_a_missed_call_with_just_the_name_listens_for_the_request(engine):
    engine.settings.set("agents.jeeves.wake_reply", "Yes?")
    engine.models.stt = lambda agent=None: FakeSTT("Jeeves.")
    engine.on_backup_utterance("microphone", LOUD * 30)
    s = engine.sessions["microphone"]
    assert s.agent_id == "jeeves" and s.mode == "request" and s.reply_at
