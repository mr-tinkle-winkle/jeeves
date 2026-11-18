"""Training by voice: "you should have responded" / "you shouldn't have responded"."""
import time

from jeeves.daemon.audio import FRAME_MS
from jeeves.functions.feedback import last_speech

LOUD, QUIET = b"\x10\x20" * 480, b"\0\0" * 480


def _audio(*parts):
    frames, voiced = [], []
    for kind, ms in parts:
        n = ms // FRAME_MS
        frames += [LOUD if kind == "talk" else QUIET] * n
        voiced += [kind == "talk"] * n
    return frames, voiced


def _ctx(engine, text, source="voice:microphone", agent="jeeves"):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    entry = new_entry(text, agent, source)
    engine.history.add(entry) if hasattr(engine.history, "add") else engine.history.entries.append(entry)
    return FunctionContext(engine, agent, engine.agents()[agent], entry)


def test_the_last_speech_before_the_call_is_found():
    frames, voiced = _audio(("talk", 1500), ("quiet", 3000), ("talk", 900), ("quiet", 400), ("talk", 1200),
                            ("quiet", 2000))
    pcm = last_speech(frames, voiced)
    seconds = len(pcm) / 32000
    assert 2.4 < seconds < 3.2             # the last two bits (a short pause between), not the first
    assert last_speech(*_audio(("quiet", 5000))) == b""
    assert last_speech(*_audio(("talk", 60), ("quiet", 2000))) == b""        # a click


def test_should_have_responded_keeps_the_missed_call_and_answers_it(engine, monkeypatch):
    frames, voiced = _audio(("quiet", 2000), ("talk", 1800), ("quiet", 1500))
    engine.before_wake["microphone"] = (time.time(), frames, voiced)
    engine.transcribe = lambda pcm, agent=None: "Jeeves, set a timer for five minutes"
    said, ran = [], []
    engine.speak = lambda ctx, text: said.append(text)
    monkeypatch.setattr(engine, "run_async", lambda fn, *a, **k: ran.append((fn.__name__, a, k)))
    res = engine.registry.get("should_have_responded").impl(_ctx(engine, "you should have responded"))
    assert res["missed"] == "Jeeves, set a timer for five minutes"
    assert res["request"] == "set a timer for five minutes"
    assert ran == [("handle_text", ("set a timer for five minutes", "jeeves"), {"source": "voice:microphone"})]
    clip = engine.clips.list()[0]
    assert clip["verdict"] == "good" and clip["outcome"].startswith("missed") and clip["heard"] == res["missed"]
    assert engine.training.recordings()[-1]["text"] == res["missed"]
    engine.settings.set("training.answer_missed", False)
    ran.clear()
    engine.registry.get("should_have_responded").impl(_ctx(engine, "you should have responded"))
    assert ran == []
    engine.before_wake["microphone"] = (time.time(), *_audio(("quiet", 3000)))
    assert engine.registry.get("should_have_responded").impl(_ctx(engine, "you should have"))["missed"] is None
    assert said[-1] == "I didn't hear anything before you called me."


def test_should_not_have_responded_teaches_from_the_last_call(engine):
    from jeeves.daemon.history import new_entry
    engine.speak = lambda ctx, text: None
    prev = new_entry("yeah the game tonight was crazy", "jeeves", "voice:microphone")
    prev["function"] = "local_response"
    engine.history.entries.append(prev)
    cid = engine.clips.add(b"\1\0" * 4800, "jeeves", "microphone", "jeeves", 0.6)
    engine.clips.update(cid, heard="Jeeves yeah the game tonight was crazy", outcome="ran as a request",
                        request=prev["id"])
    res = engine.registry.get("should_not_have_responded").impl(_ctx(engine, "you shouldn't have responded"))
    assert res["request"] == prev["id"] and "marked the wake-up as not a call" in res["learned"]
    assert next(c for c in engine.clips.list() if c["id"] == cid)["verdict"] == "bad"
    assert prev["rating"] == -1 and "shouldn't have responded" in prev["comment"]
    assert engine.settings.get("training.not_for_me") == ["yeah the game tonight was crazy"]


def test_talk_said_not_to_be_for_it_is_ignored_next_time(engine):
    from jeeves.daemon.intent import Decision
    engine.settings.set("training.not_for_me", ["yeah the game tonight was crazy"])
    ctx = _ctx(engine, "yeah the game tonight was crazy!")
    assert engine._misfire(ctx, "yeah the game tonight was crazy!", Decision("local_response", {}, 0.7, "", "model"))
    ctx = _ctx(engine, "set a timer for five minutes")
    assert not engine._misfire(ctx, "set a timer for five minutes", Decision("timer", {}, 0.9, "", "model"))


def test_the_phrases_reach_the_functions(engine):
    agent = engine.agents()["jeeves"]
    functions = engine.registry.enabled_for(agent)
    for text, want in (("you shouldn't have responded", "should_not_have_responded"),
                       ("that wasn't for you", "should_not_have_responded"),
                       ("you should have responded", "should_have_responded"),
                       ("you missed that", "should_have_responded")):
        assert engine.intent.keyword_decide(agent, text, functions).function == want, text
