"""Agents only hear their own sources; jump-in never says bare names."""
import pytest


@pytest.fixture
def two_agents(engine):
    engine.settings.set("agents.jeeves.listen_to", "user")
    from jeeves.config import default_agent
    deskbot = default_agent("Deskbot")
    deskbot["listen_to"] = "desktop"
    engine.settings.set("agents.deskbot", deskbot)
    return engine


@pytest.mark.parametrize("reply,said", [
    ("Jeeves: Sure, I can help with that.", "Sure, I can help with that."),
    ("Jeeves (you): Ha, good one.", "Ha, good one."),
    ("User", ""), ("Jeeves", ""), ("Deskbot.", ""), ("PASS", ""), ("(pass)", ""), ("I'll pass.", ""),
    ('"Jeeves, Deskbot"', ""), ("Nice!", "Nice!"),
])
def test_jump_in_never_says_just_a_name(two_agents, reply, said):
    assert two_agents.jump_in.clean(reply, two_agents.agents()["jeeves"]) == said


def test_jump_in_transcript_only_has_what_the_agent_hears(two_agents):
    j = two_agents.jump_in
    j.heard("microphone", "my bank pin is 1234")
    j.heard("desktop", "anyone up for a game")
    desk = two_agents.agents()["deskbot"]
    t = j.transcript("deskbot", desk)
    assert "anyone up for a game" in t and "bank pin" not in t
    assert "bank pin" in j.transcript("jeeves", two_agents.agents()["jeeves"])


def test_wake_on_mic_ignores_desktop_only_agent(two_agents):
    two_agents.on_wake("microphone", "deskbot", 0.99, [])
    assert two_agents.sessions.get("microphone") is None
    two_agents.on_wake("microphone", "jeeves", 0.99, [])
    assert two_agents.sessions["microphone"].agent_id == "jeeves"


def test_desktop_agent_question_does_not_open_the_mic(two_agents):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.engine import Waiter
    from jeeves.daemon.history import new_entry
    ctx = FunctionContext(two_agents, "deskbot", dict(two_agents.agents()["deskbot"]),
                          new_entry("x", "deskbot", "voice:desktop"))
    two_agents.waiters["w"] = Waiter("answer", ctx, "which one?", None)
    assert two_agents.answer_pending("microphone") is None
    assert two_agents.answer_pending("desktop") == "deskbot"
    assert not two_agents._needs_source("microphone") or two_agents.settings.get("wake_word.enabled")


def test_unnamed_voice_request_only_matches_agents_on_that_source(two_agents):
    assert two_agents.split_agent("deskbot open the menu", "microphone")[0] is None
    assert two_agents.split_agent("deskbot open the menu", "desktop")[0] == "deskbot"


def test_memory_of_mic_requests_stays_with_mic_agents(two_agents):
    from jeeves.daemon.history import new_entry
    e = new_entry("remember my locker code is 42", "jeeves", "voice:microphone")
    two_agents.history.add(e)
    two_agents.history.finish(e)
    desk = two_agents.agents()["deskbot"]
    assert two_agents.recent_for("deskbot", desk, 5) == []
    typed = new_entry("what time is it", "jeeves", "text")
    two_agents.history.add(typed)
    two_agents.history.finish(typed)
    assert [r["text"] for r in two_agents.recent_for("deskbot", desk, 5)] == ["what time is it"]


def _busy(engine, agent="jeeves"):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    entry = new_entry("read me the news", agent, "text")
    ctx = FunctionContext(engine, agent, dict(engine.agents()[agent]), entry)
    engine.active[entry["id"]] = ctx
    return ctx


@pytest.mark.parametrize("said,outcome", [
    ("", "resumed"), ("carry on", "resumed"), ("ok, go ahead", "resumed"),
    ("wait", "paused"), ("hold on a sec", "paused"),
    ("stop", "cancelled"), ("shut up", "cancelled"),
    ("what time is it", "replaced"),
])
def test_calling_a_busy_agent_pauses_it(engine, said, outcome):
    ctx = _busy(engine)
    engine.on_wake("microphone", "jeeves", 0.99, [])
    s = engine.sessions["microphone"]
    assert ctx.suspend_event.is_set() and s.interrupting == [ctx.entry["id"]]      # paused, listening
    handled = engine._after_interruption(s.interrupting, said)
    state = ("cancelled" if handled else "replaced") if ctx.cancel_event.is_set() else \
        "paused" if ctx.suspend_event.is_set() else "resumed"
    assert state == outcome
    assert handled == (outcome != "replaced")


def test_agent_saying_its_own_name_does_not_pause_it(engine):
    ctx = _busy(engine)
    ctx.saying = "Jeeves here, reading the headlines."
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert not ctx.suspend_event.is_set()


def test_paused_agent_resumes_when_called_again(engine):
    ctx = _busy(engine)
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert engine._after_interruption(engine.sessions["microphone"].interrupting, "wait")
    engine.sessions.clear()
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert engine._after_interruption(engine.sessions["microphone"].interrupting, "continue")
    assert not ctx.suspend_event.is_set()


def test_speaks_through_a_specific_device(monkeypatch):
    from jeeves.daemon import audio
    monkeypatch.setattr(audio, "ensure_virtual_mic", lambda *a, **k: True)
    t = lambda mode, dev="headset": audio.output_targets(mode, "", "jeeves-mic", True, "", dev)  # noqa: E731
    assert t("device") == ["headset"]
    assert t("device_mic") == ["headset", "jeeves-mic"]
    assert t("device", "") == [""]                 # no device chosen yet: the default output
    assert t("speakers") == [""] and t("both") == ["", "jeeves-mic"]


def _frames(voiced: int, silent: int):
    import math, struct
    loud = b"".join(struct.pack("<h", int(8000 * math.sin(i / 3))) for i in range(480))
    return [loud] * voiced + [b"\0\0" * 480] * silent


def test_pause_after_the_name_waits_for_you_to_speak(engine):
    engine.on_wake("microphone", "jeeves", 0.99, _frames(3, 30))      # a click or breath, then silence
    s = engine.sessions["microphone"]
    assert not s.got_speech                    # still waiting for the request
    import time
    assert not s.should_end(time.time() + 3, 1.2)          # a 3 s pause doesn't end it
    engine.sessions.clear()
    engine.on_wake("microphone", "jeeves", 0.99, _frames(20, 5))      # "Jeeves, open OBS" in one breath
    assert engine.sessions["microphone"].got_speech


@pytest.mark.parametrize("text,noise", [("Thank you.", True), ("Thanks for watching!", True), ("[BLANK_AUDIO]", True),
                                        ("you", True), ("open OBS", False), ("thank you, set a timer", False)])
def test_whisper_silence_hallucinations_are_ignored(text, noise):
    from jeeves.daemon.engine import is_noise_text
    assert is_noise_text(text) == noise


def test_research_explains_and_shows_clickable_sources(engine, monkeypatch):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import web
    monkeypatch.setattr(web, "search", lambda settings, q, n: [
        {"title": "Patch notes", "url": "https://example.com/a", "snippet": "s"},
        {"title": "News", "url": "https://example.org/b", "snippet": "s"}])
    monkeypatch.setattr(web, "request_website", lambda ctx, url, max_chars=4000, **kw: f"Long article text from {url}. " * 20)
    monkeypatch.setattr(engine.models, "respond", lambda *a, **k: "It's out on the 14th [1]. It fixes saves [1, 2].")
    events, said = [], []
    engine.publish = lambda topic, data: events.append((topic, data))
    engine.speak = lambda ctx, text: said.append(text)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("research", question="when is the patch")
    assert said[-1] == "It's out on the 14th. It fixes saves."           # numbers aren't read out
    src = next(d for t, d in events if t == "sources")
    assert src["answer"].endswith("[1, 2].") and [s["url"] for s in src["sources"]] == \
        ["https://example.com/a", "https://example.org/b"]
    assert "Long article text" in src["sources"][0]["text"] and ctx.entry["sources"][1]["n"] == 2
    from jeeves.overlay import sources_html
    html = sources_html(src)
    assert 'href="#s2"' in html and 'href="https://example.com/a"' in html and "show all the text read" in html


def _tone(level: float, n: int):
    import math, struct
    return [b"".join(struct.pack("<h", int(level * 32767 * 1.414 * math.sin(i / 3))) for i in range(480))] * n


def test_quiet_microphone_still_counts_as_speech(engine):
    # words at ~0.008 RMS (under the 0.012 default) after the name, over a quiet background
    after = _tone(0.0005, 10) + _tone(0.008, 25) + _tone(0.0005, 15)
    assert engine._speech_in(after) is not None
    assert engine._speech_in(_tone(0.0005, 40)) is None             # just the quiet background: not speech
    assert engine._speech_in(_tone(0.0005, 40), words=2) is None        # "words" over pure noise: not speech
    assert engine._speech_in(_tone(0.0005, 20) + _tone(0.008, 5), words=2) is not None   # words + a little speech


def test_listener_threshold_adapts_but_never_exceeds_the_setting(engine):
    from jeeves.daemon.listener import Listener
    lst = Listener(engine, "microphone")
    lst.floor = 0.0004
    assert lst.threshold() == 0.003                                # quiet mic: lower bar
    lst.floor = 0.003
    assert lst.threshold() == pytest.approx(0.009)                 # 3x the background, under the setting
    lst.floor = 0.02
    assert lst.threshold() == pytest.approx(0.032)                 # loud steady background: just above it


def test_whisper_segments_it_thinks_are_silence_are_dropped():
    from jeeves.models.backends import speech_text
    data = {"text": "What time is it? Thank you.", "segments": [
        {"text": " What time is it?", "no_speech_prob": 0.02, "avg_logprob": -0.2},
        {"text": " Thank you.", "no_speech_prob": 0.9, "avg_logprob": -1.1}]}
    assert speech_text(data) == "What time is it?"
    assert speech_text({"text": " hello "}) == "hello"


def test_steady_background_never_counts_as_endless_speech(engine):
    from jeeves.daemon.listener import Listener
    lst = Listener(engine, "microphone")
    fan = _tone(0.009, 1)[0]                         # a fan just under the default level
    voiced = 0
    for _ in range(300):
        lst.process(fan)
        voiced += lst.levels[-1] > lst.threshold()
    assert lst.floor == pytest.approx(0.009, rel=0.1) and lst.threshold() > 0.012
    assert voiced < 60                               # only while it was still learning the background


def test_a_stuck_listen_restarts_on_the_wake_word(engine):
    import time
    engine.on_wake("microphone", "jeeves", 0.99, [])
    old = engine.sessions["microphone"]
    old.started -= 20                                # open for 20 s already (stuck on noise)
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert engine.sessions["microphone"] is not old and old.ended
    fresh = engine.sessions["microphone"]
    engine.on_wake("microphone", "jeeves", 0.99, [])  # a young listen isn't interrupted
    assert engine.sessions["microphone"] is fresh


def test_too_little_speech_is_not_a_request(engine):
    from jeeves.daemon.listener import Session
    s = engine.open_session("microphone", "jeeves", "request")
    s.got_speech, s.voiced_frames = True, 4          # 0.12 s: a click or a cough
    engine.end_session(s)
    assert not s.got_speech


def test_unsure_wake_is_checked_by_speech_recognition(engine, monkeypatch):
    import time
    engine.run_async = lambda fn, *a: fn(*a)                         # run the check right away
    heard = {"text": "hey jeeves what time is it"}
    monkeypatch.setattr(engine, "transcribe", lambda pcm, agent=None: heard["text"])
    engine.on_wake("microphone", "jeeves", 0.4, [])                 # under 0.6 but over half of it
    assert engine.sessions.get("microphone") is not None             # STT heard the name: awake
    engine.sessions.clear()
    heard["text"] = "the weather is nice"
    engine.on_wake("microphone", "jeeves", 0.4, [])
    assert engine.sessions.get("microphone") is None                 # not the name: ignored
    engine.on_wake("microphone", "jeeves", 0.2, [])                  # way too unsure: not even checked
    assert engine.sessions.get("microphone") is None


def test_a_dropped_listen_says_so_on_the_indicator(engine):
    s = engine.open_session("microphone", "jeeves", "request")
    s.got_speech, s.voiced_frames = True, 3
    engine.end_session(s)
    assert engine.indicators[s.request_id]["stage"] == "unclear"


def test_screen_reading_shows_the_screens_to_a_vision_model(engine, monkeypatch):
    from jeeves.daemon import watcher
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    import tempfile, pathlib
    shot = pathlib.Path(tempfile.mkstemp(suffix=".png")[1])
    monkeypatch.setattr(watcher, "grab", lambda screen: {"images": [("your left screen", b"L"), ("your right screen", b"R")],
                                                         "shot": shot})
    monkeypatch.setattr(engine.registry.get("read_screen_text"), "impl", lambda ctx, region="anywhere", screen="all": "OK")
    seen = {}

    class Vision:
        def chat(self, messages, **kw):
            seen["messages"] = messages
            return "Your right screen shows a disk-full error."
    monkeypatch.setattr(engine.models, "vision_llm", lambda agent=None: Vision())
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("screen_reading", question="what's on my screens")
    parts = seen["messages"][-1]["content"]
    assert [p["type"] for p in parts].count("image_url") == 2 and "your left screen" in parts[-1]["text"]
    assert said == ["Your right screen shows a disk-full error."]
