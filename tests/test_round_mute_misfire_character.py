"""Muting agents, misfires, research that asks when stuck, voice previews, stopping speech, and every
line in character."""
import threading


def _said(engine):
    out = []
    engine.speak = lambda ctx, t: out.append(t)
    return out


def test_audio_mute_says_nothing_and_visual_mute_hides_it(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.engine import Engine
    from jeeves.daemon.history import new_entry
    events = []
    engine.publish = lambda t, d: events.append((t, d))
    engine._synth = lambda *a: (_ for _ in ()).throw(AssertionError("muted: nothing synthesized"))
    engine.speak = Engine.speak.__get__(engine)
    engine.settings.set("agents.jeeves.mute", {"audio": True, "visual": True})
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.say("Timer set for 2:00.")
    assert any(t["kind"] == "muted" for t in ctx.entry["trace"])
    shown = [d for t, d in events if t == "response"]
    assert shown and all(d["hidden"] for d in shown)
    engine.set_indicator("r1", "jeeves", "thinking")
    engine.set_indicator("r2", "jeeves", "asking", "Which one?")
    ind = {d["request"]: d for t, d in events if t == "indicator"}
    assert ind["r1"].get("hidden") and not ind["r2"].get("hidden")       # asking you still shows


def test_a_wake_word_followed_by_chatter_is_ignored(engine):
    said = _said(engine)

    def run(text, source):
        return engine.history.get(engine.handle_text(text, "jeeves", source=source, wait=True)["id"])
    e = run("yeah I guess man, the game tonight was crazy", "voice:microphone")
    assert e["status"] == "ignored" and not said
    assert run("yeah I guess man, the game tonight was really crazy", "text")["status"] != "ignored"   # typed: meant
    engine.settings.set("general.ignore_chatter", False)
    assert run("lol no way", "voice:microphone")["status"] != "ignored"                          # option off


def test_the_intent_model_can_say_it_was_not_for_the_agent(engine):
    from jeeves.daemon.intent import IntentProcessor

    class LLM:
        def loaded(self):
            return True

        def chat(self, messages, **kw):
            assert "not_for_me" in messages[0]["content"]
            return '{"function": null, "not_for_me": true}'

    class M:
        suspended = {}

        def llm(self, kind, agent=None):
            return LLM()
    d = IntentProcessor(engine.settings, engine.registry, M(), engine.history).decide(
        engine.agents()["jeeves"], "the match tonight was something else")
    assert d.function is None and d.not_for_me


def test_research_tries_other_spellings_then_asks(engine, monkeypatch):
    from jeeves.functions import builtins, research
    said = _said(engine)
    asked = []
    looked_up = []

    def fake_run(ctx, q, depth=None, earlier="", plan_out=None):
        looked_up.append(q)
        if plan_out is not None:
            plan_out.update({"goal": q, "context": [], "terms": [q.split()[-1]]})
        if "wumpy" in q.lower():
            return [{"title": "Wumpy - Wiki", "url": "https://w/wumpy", "text": "A Wumpy is a jump."}], [], []
        return [{"title": "Something else", "url": "https://x", "text": "nothing relevant"}], [], []
    monkeypatch.setattr(research, "run", fake_run)
    monkeypatch.setattr(research, "spelling_variants", lambda ctx, t, q: ["Wumpey"])
    monkeypatch.setattr(research, "related_names", lambda ctx, t, plan: [])
    engine.wait_for_answer = lambda ctx, q, choices, timeout: asked.append(q) or "W U M P Y"
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("what is a Wumpie", "jeeves", "text"))
    sources, _ = builtins._research_until_found(ctx, "what is a Wumpie", "", "", {})
    assert looked_up[:2] == ["what is a Wumpie", "what is a Wumpey"]            # the other spelling first
    assert asked and "spelled" in asked[0] and looked_up[-1].lower() == "what is a wumpy"
    assert sources and "Wumpy" in sources[0]["title"]
    del said


def test_research_gives_up_after_asking(engine, monkeypatch):
    from jeeves.functions import builtins, research
    said = _said(engine)
    monkeypatch.setattr(research, "run", lambda ctx, q, depth=None, earlier="", plan_out=None: (
        plan_out.update({"goal": q, "context": [], "terms": ["Zorbleflux"]}) if plan_out is not None else None,
        ([], [], []))[1])
    monkeypatch.setattr(research, "spelling_variants", lambda ctx, t, q: [])
    monkeypatch.setattr(research, "related_names", lambda ctx, t, plan: [])
    asked = []
    engine.wait_for_answer = lambda ctx, q, choices, timeout: asked.append(q) or "no idea"
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("what is Zorbleflux", "jeeves", "text"))
    assert builtins._research_until_found(ctx, "what is Zorbleflux", "", "", {}) == (None, None)
    assert len(asked) == 2 and "spelled" in asked[0] and "more" in asked[1]
    assert "couldn't find anything" in said[-1]


def test_spellings_as_said():
    from jeeves.functions.research import spelled
    assert spelled("W U M P Y") == "Wumpy"
    assert spelled("it's w-u-m-p-y.").lower() == "wumpy"
    assert spelled("Wumpy") == "Wumpy"


def test_nothing_wakes_during_a_voice_preview(engine, monkeypatch):
    from jeeves.daemon import engine as eng_mod
    started = threading.Event()
    release = threading.Event()

    class SlowPlayback:
        def __init__(self, pcm, rate, targets):
            self.error, self.pcm, self.rate = None, pcm, rate

        def seconds(self):
            return 5.0

        def play(self):
            started.set()
            release.wait(5)
    monkeypatch.setattr(eng_mod, "Playback", SlowPlayback)
    engine._synth = lambda agent, text: (b"\0" * 3200, 16000)
    threading.Thread(target=engine.preview_voice, args=(engine.agents()["jeeves"],), daemon=True).start()
    assert started.wait(5)
    assert engine.previewing()
    opened = []
    monkeypatch.setattr(engine, "open_session", lambda *a, **k: opened.append(a))
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert not opened
    release.set()


def test_abort_stops_every_voice(engine):
    from jeeves.daemon import audio
    pb = audio.Playback(b"\0" * 32000, 16000, [""])
    with audio._live_lock:
        audio.LIVE.add(pb)
    engine.abort()
    assert pb.stopped.is_set()
    with audio._live_lock:
        audio.LIVE.discard(pb)


def test_plain_lines_are_said_in_character_keeping_the_facts(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    engine.settings.set("persona.every_line", True)
    said = _said(engine)
    answers = ["Hark! The sands of two minutes and naught seconds -- 2:00 -- now flow at my command!",
               "Behold, the hourglass is set!"]
    engine.models.respond = lambda agent, line, **kw: answers.pop(0)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.say("Timer set for 2:00.")
    assert "2:00" in said[-1] and said[-1].startswith("Hark")
    ctx.say("Timer set for 3:00.")                         # the rewrite lost the time: the plain line
    assert said[-1] == "Timer set for 3:00."
    ctx.say("Here's what I think.", in_character=True)     # already its own words: as is
    assert said[-1] == "Here's what I think."
    engine.settings.set("agents.jeeves.persona_every_line", False)
    ctx.agent = engine.agents()["jeeves"]
    ctx.say("Timer set for 4:00.")
    assert said[-1] == "Timer set for 4:00."


def test_research_looks_for_what_you_may_have_meant(engine, monkeypatch):
    from jeeves.functions import builtins, research
    said = _said(engine)
    monkeypatch.setattr(research, "search_many", lambda ctx, qs, count=6: [
        {"title": "Wumpy Jump - Parkour Reborn Wiki", "url": "https://w/1", "snippet": "The Wumpy Jump is a trick."},
        {"title": "Wumpus (game)", "url": "https://w/2", "snippet": "Hunt the Wumpus"}])
    assert research.related_names(None, "Wumpie", {"context": ["Parkour Reborn"]})[0] in ("Wumpy", "Wumpy Jump")

    def fake_run(ctx, q, depth=None, earlier="", plan_out=None):
        if plan_out is not None:
            plan_out.update({"goal": q, "context": ["Parkour Reborn"], "terms": [q.split()[-1]]})
        if "wumpy" in q.lower():
            return [{"title": "Wumpy Jump", "url": "https://w/1", "text": "The Wumpy Jump is a trick."}], [], []
        return [], [], []
    monkeypatch.setattr(research, "run", fake_run)
    monkeypatch.setattr(research, "spelling_variants", lambda ctx, t, q: [])
    engine.wait_for_answer = lambda *a, **k: (_ for _ in ()).throw(AssertionError("found it: no need to ask"))
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("what is a Wumpie", "jeeves", "text"))
    sources, _ = builtins._research_until_found(ctx, "what is a Wumpie", "", "", {})
    assert sources and "think you mean" in said[-1]


def _ctx(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    return FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))


def test_lines_said_often_are_reworded_without_the_model(engine):
    from jeeves.daemon import engine as eng_mod
    engine.settings.set("persona.every_line", True)
    said = _said(engine)
    calls = []

    def respond(agent, line, **kw):
        calls.append(line)
        return f"Variant {len(calls)}: it is done."
    engine.models.respond = respond
    ctx = _ctx(engine)
    for _ in range(eng_mod.CHARACTER_VARIANTS + 4):
        ctx.say("Done.")
    assert len(calls) == eng_mod.CHARACTER_VARIANTS          # then reused: no more model calls
    assert all(s.startswith("Variant") for s in said)
    ctx.say("Paused.")                                       # another line still goes to the model
    assert len(calls) == eng_mod.CHARACTER_VARIANTS + 1


def test_a_model_too_slow_for_rewording_says_the_plain_line(engine, monkeypatch):
    from jeeves.daemon import engine as eng_mod
    engine.settings.set("persona.every_line", True)
    engine.settings.set("persona.every_line_max_seconds", 2.0)
    said = _said(engine)
    clock = [1000.0]
    monkeypatch.setattr(eng_mod.time, "time", lambda: clock[0])
    calls = []

    def respond(agent, line, **kw):
        calls.append(line)
        clock[0] += 9.0                                      # CPU-only: nine seconds per rewording
        return f"Verily: {line}"
    engine.models.respond = respond
    ctx = _ctx(engine)
    for n in range(2):
        ctx.say(f"Line {n}.")
    assert len(calls) == 2
    ctx.say("Line 2.")
    assert said[-1] == "Line 2." and len(calls) == 2         # two slow ones: plain from here on
    assert any(t["kind"] == "in_character_skipped" for t in ctx.entry["trace"])
    for n in range(3, 3 + eng_mod.CHARACTER_RETRY_EVERY):
        ctx.say(f"Line {n}.")
    assert len(calls) == 3                                   # ...but it's tried again now and then
    engine.settings.set("persona.every_line_max_seconds", 0)
    ctx.say("Line 99.")
    assert said[-1] == "Verily: Line 99."                    # 0 = always reword


def test_long_replies_are_split_at_sentences_for_speaking():
    from jeeves.daemon.engine import resample, speech_chunks
    assert speech_chunks("Timer set for 2:00.") == ["Timer set for 2:00."]
    long = ("The Eiffel Tower is 330 metres tall. It was finished in 1889 for the World's Fair. "
            "About 7 million people visit it each year, e.g. from the U.S. and Japan. It is repainted every seven years.")
    parts = speech_chunks(long)
    assert len(parts) >= 2 and " ".join(parts) == long
    assert parts[0] == "The Eiffel Tower is 330 metres tall. It was finished in 1889 for the World's Fair."
    one = "a " * 200
    assert speech_chunks(one) == [one.strip()]               # one sentence: whole
    assert len(resample(b"\x01\x00" * 100, 16000, 8000)) == 100


def test_a_long_reply_starts_playing_before_the_rest_is_synthesized(engine, monkeypatch):
    from jeeves.daemon import audio, engine as eng_mod
    made, fed = [], []
    gate = threading.Event()

    def synth(agent, text):
        made.append(text)
        if len(made) > 1:
            gate.wait(5)                                     # the rest is slow to make
        return (b"\x10\x20" * 800 * len(made), 16000)
    engine._synth = synth

    class Proc:
        pid, returncode, args = 1, 0, ["fake"]

        class stdin:
            @staticmethod
            def write(b):
                fed.append(b)
                if len(b"".join(fed)) >= 1600:
                    gate.set()                               # the opening is already playing: now the rest arrives

            flush = close = staticmethod(lambda: None)
        stderr = None
        wait = terminate = kill = lambda self, **k: 0
    monkeypatch.setattr(audio.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(audio, "_drain", lambda p, out: None)
    monkeypatch.setattr(audio, "_player", lambda *a, **k: ["fake"])
    monkeypatch.setattr(eng_mod.Engine, "voice_targets", lambda self, agent: [""])
    ctx = _ctx(engine)
    text = ("The Eiffel Tower is 330 metres tall and stands in Paris. It was finished in 1889 for the World's Fair. "
            "About 7 million people visit it each year. It is repainted every seven years by hand.")
    engine.speak(ctx, text)
    assert len(made) >= 2 and " ".join(made) == text
    assert len(b"".join(fed)) == sum(1600 * n for n in range(1, len(made) + 1))     # every part was played
    speech = next(t for t in ctx.entry["trace"] if t["kind"] == "speech")
    assert speech["parts"] == len(made)
    engine.settings.set("audio.stream_speech", False)
    made.clear()
    fed.clear()
    engine.speak(ctx, text)
    assert made == [text]
