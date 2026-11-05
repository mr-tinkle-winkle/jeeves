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
