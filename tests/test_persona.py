"""The agent's prompt actually shapes what the model is asked."""


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, max_tokens=512, temperature=0.6, json_mode=False, on_token=None, cancelled=None, **kw):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "4: fine"


def _agent(engine, **kw):
    a = dict(engine.agents()["jeeves"], id="jeeves")
    a.update(prompt="You are Jeeves, a dry-witted, formal British butler. Call the user sir.", **kw)
    return a


def test_character_comes_first_and_the_request_is_left_as_said(engine):
    msgs = engine.models.build_messages(_agent(engine), "what's the weather like?")
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("You are Jeeves, a dry-witted")
    assert "Stay in character" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "what's the weather like?"}     # nothing tacked on


def test_raw_requests_have_no_character(engine):
    msgs = engine.models.build_messages(_agent(engine), "plan clicks", raw=True)
    assert msgs == [{"role": "user", "content": "plan clicks"}]


def test_other_agents_replies_are_not_its_own_turns(engine):
    from jeeves.config import default_agent
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    engine.settings.set("agents.friday", default_agent("Friday", prompt="Bubbly and casual."))
    for agent, text, result in (("friday", "tell me a joke", "omg ok so like"), ("jeeves", "time?", "Noon, sir.")):
        e = new_entry(text, agent, "text")
        e["response"], e["function"] = result, "local_response"
        engine.history.add(e)
        engine.history.finish(e)
    a = _agent(engine)
    ctx = FunctionContext(engine, "jeeves", a, new_entry("x", "jeeves", "text"))
    msgs = engine.models.build_messages(a, "and now?", ctx=ctx, with_memory=True)
    assistant = [m["content"] for m in msgs if m["role"] == "assistant"]
    assert assistant == ["Noon, sir."]                      # only its own past replies
    assert "omg ok so like" not in msgs[0]["content"]        # Friday's only when asked about
    msgs = engine.models.build_messages(a, "what did Friday just say?", ctx=ctx, with_memory=True)
    assert "omg ok so like" in msgs[0]["content"] and not [m for m in msgs if m["role"] == "assistant"]


def test_earlier_turns_only_for_a_follow_up(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    for text, fn, result in (("set a timer for 5 minutes", "timers", "Timer set."),
                             ("what's the tallest mountain", "research", "Everest, sir [1].")):
        e = new_entry(text, "jeeves", "text")
        e["response"], e["function"] = result, fn
        engine.history.add(e)
        engine.history.finish(e)
    a = _agent(engine)
    ctx = FunctionContext(engine, "jeeves", a, new_entry("x", "jeeves", "text"))
    fresh = engine.models.build_messages(a, "tell me a joke about cheese", ctx=ctx, with_memory=True)
    assert [m["role"] for m in fresh] == ["system", "user"]
    follow = engine.models.build_messages(a, "why is that?", ctx=ctx, with_memory=True)
    assert follow[1:] == [{"role": "user", "content": "what's the tallest mountain"},
                          {"role": "assistant", "content": "Everest, sir."},       # citation marks gone
                          {"role": "user", "content": "why is that?"}]


def test_only_notes_about_the_request_are_shown(engine):
    for note in ("my dog is called Biscuit", "I play Satisfactory on weekends", "the wifi password is in the drawer"):
        engine.memory.add(note, permanent=True, agent="jeeves")
    assert engine.memory.context_for("jeeves", about="tell me a joke about cheese") == ""
    dog = engine.memory.context_for("jeeves", about="what's my dog called?")
    assert "Biscuit" in dog and "Satisfactory" not in dog and "wifi" not in dog
    everything = engine.memory.context_for("jeeves", about="what do you remember about me?")
    assert all(w in everything for w in ("Biscuit", "Satisfactory", "wifi"))


def test_follow_ups_are_told_apart_from_whole_questions():
    from jeeves.util import refers_back
    for t in ("why is that", "why?", "tell me more", "play it again", "and the second one?", "is he still alive",
              "what about Germany", "adjust the macro I just made"):
        assert refers_back(t), t
    for t in ("tell me a joke about cheese", "what time is it", "how do I do a wumpy in parkour reborn",
              "what are the chances an average player beats a white knuckle campaign on their first try",
              "is it going to rain tomorrow", "how old is tom cruise and what is his latest movie"):
        assert not refers_back(t), t


def test_out_of_character_reply_is_rewritten(engine, monkeypatch):
    llm = FakeLLM(["Sure! As an AI, I'd love to help!", "1: generic assistant", "Very good, sir. Consider it done."])
    monkeypatch.setattr(engine.models, "llm", lambda kind, agent=None: llm)
    reply = engine.models.respond(_agent(engine, persona_check=True), "book a table")
    assert reply == "Very good, sir. Consider it done." and len(llm.calls) == 3
    llm2 = FakeLLM(["Very good, sir.", "5: formal butler"])
    monkeypatch.setattr(engine.models, "llm", lambda kind, agent=None: llm2)
    assert engine.models.respond(_agent(engine, persona_check=True), "thanks") == "Very good, sir."
    assert len(llm2.calls) == 2                               # in character: kept as is


def test_personality_test_grades_answers(engine, monkeypatch):
    replies = []
    for _ in range(4):
        replies += ["Indeed, sir.", "5: formal"]
    llm = FakeLLM(replies)
    monkeypatch.setattr(engine.models, "llm", lambda kind, agent=None: llm)
    res = engine.models.test_persona(_agent(engine))
    assert res["average"] == 5 and len(res["results"]) == 4 and res["verdict"] == "stays in character"


def test_jeenius_scale():
    from jeeves.models import jeenius
    from jeeves.config import Settings
    s = Settings()
    assert jeenius.level({}, s) == 2 and jeenius.level({"jeenius": 4}, s) == 4
    t = jeenius.think_for
    assert t(1, "answer", "explain why the sky is blue") is False
    assert t(2, "chat", "hi there") is False and t(2, "chat", "explain why the sky is blue") is True
    assert t(2, "answer", "who won") is True and t(2, "step", "explain why") is False
    assert t(3, "chat", "thanks") is False and t(3, "chat", "what's a good name for my cat") is True
    assert t(4, "chat", "hi") == "high" and t(4, "step", "x") is True


def test_thinking_is_asked_for_per_request():
    from jeeves.models.backends import LlamaCppLLM
    from types import SimpleNamespace
    llm = LlamaCppLLM(SimpleNamespace(id="m", files=[]), reasoning="auto")
    assert llm.extra_body(False)["chat_template_kwargs"] == {"reasoning_effort": "low", "enable_thinking": False}
    assert llm.extra_body("high")["chat_template_kwargs"]["reasoning_effort"] == "high"
