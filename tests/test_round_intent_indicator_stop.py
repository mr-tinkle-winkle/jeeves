"""The spinner shows from the intent step; "the newest, most critical video"; stopping doesn't hang."""
import time


class STT:
    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm, prompt="", language="en"):
        return self.text


def _events(engine):
    ev = []
    engine._publish_fn = lambda t, d: ev.append((t, d))
    return ev


def test_voice_request_shows_thinking_before_anything_runs(engine, monkeypatch):
    ev = _events(engine)
    started = []
    monkeypatch.setattr(engine, "_run", lambda ctx, skip: started.append(
        [d["stage"] for t, d in ev if t == "indicator" and d["request"] == ctx.entry["id"]]))
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("what time is it"))
    s = engine.open_session("microphone", "jeeves", "request")
    s.feed(b"\0" * 960, True, time.time())
    engine._finish_session(s)
    for _ in range(100):
        if started:
            break
        time.sleep(0.02)
    stages = started[0]
    assert stages.index("transcript") < stages.index("thinking")    # on before the request even runs


def test_not_a_request_clears_the_spinner(engine, monkeypatch):
    ev = _events(engine)
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("set a timer for 6 minutes"))
    s = engine.open_session("microphone", None, "request")         # no agent named in it
    s.feed(b"\0" * 960, True, time.time())
    engine._finish_session(s)
    mine = [d["stage"] for t, d in ev if t == "indicator" and d["request"] == s.request_id]
    assert mine[-1] == "idle"


def test_commas_from_speech_recognition_dont_hide_the_channel():
    from jeeves.functions.partials.youtube import parse_request
    assert parse_request("pull up the newest, most critical video") == \
        {"channel": "most critical", "query": "", "newest": True}


def test_a_misheard_channel_name_still_sounds_right():
    from jeeves.functions.partials.youtube import _name_score, sound_key
    assert sound_key("most critical") == sound_key("Moist Critikal")
    assert _name_score("most critical", "Moist Critikal") >= 0.8
    assert _name_score("most critical", "CNN") < 0.3


def test_newest_without_a_channel_tries_the_words_as_one(monkeypatch):
    from jeeves.functions.partials import youtube as yt
    monkeypatch.setattr(yt, "find_channel", lambda name: ("UCmoist", "Moist Critikal"))
    monkeypatch.setattr(yt, "channel_uploads", lambda cid: [{"title": "newest", "channel": "Moist Critikal"}])
    monkeypatch.setattr(yt, "search", lambda q, n=10: [{"title": "CNN news", "channel": "CNN"}])
    assert yt.pick("the newest, most critical", "", True)["channel"] == "Moist Critikal"
    monkeypatch.setattr(yt, "find_channel", lambda name: ("UCcnn", "CNN"))   # no channel sounds like it
    assert yt.pick("the newest, most critical", "", True)["channel"] == "CNN"


def test_stopping_is_bounded(monkeypatch):
    from jeeves.daemon import server
    assert server.STOP_SECONDS <= 8
    exited = []
    monkeypatch.setattr(server.os, "_exit", exited.append)
    monkeypatch.setattr(server.logging, "shutdown", lambda: None)
    server._exit_after(0.05)
    time.sleep(0.3)
    assert exited == [0]


class _Spotter:
    def quick_text(self, pcm):
        return "jeeves set a timer"


def test_summary_keeps_the_wake_word_model(engine, monkeypatch):
    engine.settings.set("summary.enabled", True)
    engine.settings.set("summary.sources", ["microphone"])
    monkeypatch.setattr(engine.models, "wake_spotter", lambda: _Spotter())
    modes = engine.detection_modes("microphone")
    assert "vosk" in modes and "transcribe" in modes


def test_a_call_the_wake_model_heard_isnt_run_twice(engine, monkeypatch):
    engine.settings.set("summary.enabled", True)
    monkeypatch.setattr(engine.models, "wake_spotter", lambda: _Spotter())

    class SlowSTT(STT):                                  # Whisper on the CPU: seconds
        def transcribe(self, pcm, prompt="", language="en"):
            time.sleep(4)
            return self.text
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: SlowSTT("Jeeves, set a timer for 3 minutes"))
    engine._wakes["microphone"] = [time.time() - 1.5]   # the wake model called while it was being said
    engine.on_utterance("microphone", b"\0" * 32000 * 2)
    time.sleep(0.3)
    assert not engine.timers.list()
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("Jeeves, set a timer for 3 minutes"))
    engine._wakes["microphone"] = []                    # the wake model missed it: the transcript calls
    engine.on_utterance("microphone", b"\0" * 32000 * 2)
    deadline = time.time() + 5
    while time.time() < deadline and not engine.timers.list():
        time.sleep(0.02)
    assert engine.timers.list()


def test_summary_alone_doesnt_run_whisper(engine, monkeypatch):
    engine.settings.set("summary.enabled", True)
    monkeypatch.setattr(engine.models, "wake_spotter", lambda: type("S", (), {"quick_text": lambda self, p: "nice weather today"})())
    ran = []
    monkeypatch.setattr(engine, "transcribe", lambda pcm, *a, **k: ran.append(1) or "nice weather today")
    engine.on_utterance("microphone", b"\0" * 32000)
    assert "nice weather" in engine.summary.text(5) and not ran


def test_nested_names_are_one_call():
    from jeeves.models.backends import WakeRecognizer as W
    names = ["jeeves", "evil jeeves"]
    assert W.pick_names([("evil", 0.9), ("jeeves", 0.95)], names) == [("evil jeeves", 0.925)]
    assert W.pick_names([("evil", 0.3), ("jeeves", 0.95)], names) == [("jeeves", 0.95)]   # "hey jeeves"
    assert W.pick_names([("jeeves", 0.9)], names) == [("jeeves", 0.9)]


def test_the_transcript_decides_between_jeeves_and_evil_jeeves(engine):
    engine.settings.set("agents.evil_jeeves", {"name": "Evil Jeeves", "call_names": ["evil jeeves"], "enabled": True})
    assert engine.named_agent("Evil Jeeves, what time is it?") == "evil_jeeves"
    assert engine.named_agent("Hey Jeeves, what time is it?") == "jeeves"
    assert engine.split_agent("Evil Jeeves, set a timer")[:2] == ("evil_jeeves", "set a timer")
    assert engine.split_agent("Jeeves, set a timer")[:2] == ("jeeves", "set a timer")


def test_a_mixed_up_wake_goes_to_the_agent_named(engine, monkeypatch):
    engine.settings.set("agents.evil_jeeves", {"name": "Evil Jeeves", "call_names": ["evil jeeves"], "enabled": True})
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("Evil Jeeves, set a timer for 4 minutes"))
    went = []
    monkeypatch.setattr(engine, "handle_text", lambda text, aid=None, **k: went.append((aid, text)) or {})
    s = engine.open_session("microphone", "jeeves", "request")        # the wake model said "jeeves"
    s.preroll = [b"\0" * 960]
    s.feed(b"\0" * 960, True, time.time())
    engine._finish_session(s)
    assert went == [("evil_jeeves", "set a timer for 4 minutes")]


def test_a_short_sound_isnt_a_confident_name():
    from jeeves.models.backends import WakeRecognizer as W
    juh = W.pick_names([("jeeves", 1.0, 0.12)], ["jeeves"])[0][1]
    real = W.pick_names([("jeeves", 1.0, 0.42)], ["jeeves"])[0][1]
    assert juh < 0.3 and real == 1.0


def test_a_rudimentary_agent_told_just_a_name_runs_it(engine):
    from jeeves.daemon.intent import IntentProcessor
    from jeeves.functions.base import Arg, FunctionDef
    from tests.test_frame_rudimentary_proxy import _LLM, _Models
    f = FunctionDef(name="launch_game", kind="full", title="Launch game", description="Starts a Steam game.",
                    args=[Arg("game", "string", "which game")], impl=lambda ctx, game: game)
    llm = _LLM(['{"question": "What game do you want to start?"}'])
    ip = IntentProcessor(engine.settings, engine.registry, _Models(llm), engine.history)
    d = ip.decide_one(engine.agents()["jeeves"], f, "White Knuckle.")
    assert d.function == "launch_game" and d.args == {"game": "White Knuckle"} and not d.question


def test_a_device_that_is_the_mic_gets_no_second_listener(engine, monkeypatch):
    engine.settings.set("agents.jeeves.listen_to", "device")
    engine.settings.set("agents.jeeves.listen_device", "alsa_input.usb-ZOOM")
    monkeypatch.setattr(engine, "_is_the_mic", lambda dev: dev == "alsa_input.usb-ZOOM")
    monkeypatch.setattr(engine, "is_on", lambda: True)
    wanted = engine._wanted_sources()
    assert "device:alsa_input.usb-ZOOM" not in wanted and "microphone" in wanted
    assert engine._listens(engine.agents()["jeeves"], "microphone")
