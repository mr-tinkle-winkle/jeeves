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
