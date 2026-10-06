"""The agent's prompt actually shapes what the model is asked."""


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, max_tokens=512, temperature=0.6, json_mode=False, on_token=None, cancelled=None):
        self.calls.append(messages)
        return self.replies.pop(0) if self.replies else "4: fine"


def _agent(engine, **kw):
    a = dict(engine.agents()["jeeves"], id="jeeves")
    a.update(prompt="You are Jeeves, a dry-witted, formal British butler. Call the user sir.", **kw)
    return a


def test_character_comes_first_and_is_repeated_last(engine):
    msgs = engine.models.build_messages(_agent(engine), "what's the weather like?")
    assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("You are Jeeves.")
    assert "<character>" in msgs[0]["content"] and "takes priority" in msgs[0]["content"]
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"].endswith("(Reply as Jeeves, fully in character.)")


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
        e["response"] = result
        engine.history.add(e)
        engine.history.finish(e)
    a = _agent(engine)
    ctx = FunctionContext(engine, "jeeves", a, new_entry("x", "jeeves", "text"))
    msgs = engine.models.build_messages(a, "and now?", ctx=ctx, with_memory=True)
    assistant = [m["content"] for m in msgs if m["role"] == "assistant"]
    assert assistant == ["Noon, sir."]                      # only its own past replies
    assert "omg ok so like" in msgs[0]["content"] and "not you" in msgs[0]["content"]


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
