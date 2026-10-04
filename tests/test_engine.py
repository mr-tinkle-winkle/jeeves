import json
import socket
import threading
import time

import pytest

from jeeves import paths
from jeeves.daemon.intent import IntentProcessor
from jeeves.util import parse_clock, parse_duration


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- util
@pytest.mark.parametrize("text,seconds", [("5 minutes", 300), ("1h30m", 5400), ("90", 90), ("half an hour", 1800),
                                          ("ten minutes", 600), ("2:30", 150), ("1.5 hours", 5400), (45, 45)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_clock():
    import datetime as dt
    now = dt.datetime(2026, 10, 4, 18, 0)
    assert parse_clock("7:30 pm", now) == dt.datetime(2026, 10, 4, 19, 30)
    assert parse_clock("9am", now) == dt.datetime(2026, 10, 5, 9, 0)        # already passed today
    assert parse_clock("tomorrow 8", now) == dt.datetime(2026, 10, 5, 8, 0)
    assert parse_clock("in 10 minutes", now) == now + dt.timedelta(minutes=10)


# ---------------------------------------------------------------- agents & intent
def test_split_agent_exact_and_fuzzy(engine):
    engine.settings.set("agents.claude", {"name": "Claude", "call_names": ["Claude"]})
    assert engine.split_agent("Jeeves, open all my apps")[:2] == ("jeeves", "open all my apps")
    assert engine.split_agent("hey claude what is the meaning of life")[:2] == ("claude", "what is the meaning of life")
    aid, rest, score = engine.split_agent("Jeeve, set a timer")       # STT dropped a letter
    assert aid == "jeeves" and rest == "set a timer"
    assert engine.split_agent("open all my apps")[0] is None


def test_keyword_intent_picks_functions(engine):
    agent = dict(engine.agents()["jeeves"], functions={"online_prompt": True, "macros": True})
    d = engine.intent.decide(agent, "set a timer for ten minutes for the pasta")
    assert d.function == "timers" and d.args["duration"] == "ten minutes" and d.args["label"] == "the pasta"
    d = engine.intent.decide(agent, "ask gemini what the tallest building is")
    assert d.function == "online_prompt" and d.args["agent"] == "gemini"
    d = engine.intent.decide(agent, "make a macro that spams left click")
    assert d.function == "macros" and d.args["action"] == "create"
    d = engine.intent.decide(agent, "what is the meaning of life")
    assert d.function == "local_response"


def test_blocked_statements_exclude_a_function(engine):
    engine.settings.set("functions.blocked.timers", ["timer for the pasta"])
    d = engine.intent.decide(engine.agents()["jeeves"], "set a timer for the pasta")
    assert d.function == "local_response"


def test_validate_enforces_choices(engine):
    f = engine.registry.get("timers")
    args, problems = engine.intent.validate(f, {"action": "TIMER", "duration": "5 minutes", "bogus": 1})
    assert args["action"] == "timer" and "bogus" not in args and not problems
    _, problems = engine.intent.validate(f, {"action": "explode"})
    assert problems


class FakeLLM:
    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def loaded(self):
        return True

    def chat(self, messages, **kw):
        self.seen.append(messages)
        return self.answers.pop(0)


class FakeModels:
    def __init__(self, llm):
        self._llm = llm
        self.suspended = {}

    def llm(self, kind, agent=None):
        return self._llm


def test_model_intent_retries_on_invalid_answer(engine):
    llm = FakeLLM(['{"function": "timers", "args": {"action": "nuke"}}',
                   '{"function": "timers", "args": {"action": "timer", "duration": "3 minutes"}, "confidence": 0.9}'])
    ip = IntentProcessor(engine.settings, engine.registry, FakeModels(llm), engine.history)
    d = ip.decide(engine.agents()["jeeves"], "three minute timer")
    assert d.method == "model" and d.function == "timers" and d.args["duration"] == "3 minutes"
    assert "must be one of" in llm.seen[1][-1]["content"]
    system = llm.seen[0][0]["content"]
    assert "### timers" in system and "### macros" not in system     # only enabled functions


# ---------------------------------------------------------------- pipeline
def test_dry_run_runs_nothing(engine):
    e = engine.dry_run("Jeeves, set a timer for 2 minutes")
    assert e["function"] == "timers" and e["status"] == "dry_run"
    assert engine.timers.list() == []
    assert engine.history.list() == []       # dry runs aren't history


def test_text_request_runs_and_records_history(engine):
    res = engine.handle_text("Jeeves, set a timer for 2 minutes", wait=True)
    entry = engine.history.get(res["id"])
    assert entry["status"] == "done" and entry["response"] == "Timer set for 2:00."
    assert len(engine.timers.list()) == 1
    assert any(t["kind"] == "say" for t in entry["trace"])
    assert engine.history.recent(3)[-1]["function"] == "timers"


def test_text_request_without_agent_name_is_refused(engine):
    assert "error" in engine.handle_text("set a timer for 2 minutes")


def test_memory_phrases_are_committed(engine):
    engine.handle_text("Jeeves, set a timer for 1 minute, remember forever", wait=True)
    assert any(n["permanent"] for n in engine.memory.all())
    assert (paths.data_dir() / "permanent_memory.json").exists()


def test_unclear_requests_ask_and_abort_stops_waiting(engine):
    engine.settings.set("agents.jeeves.functions", {"local_response": False})
    res = engine.handle_text("Jeeves, frobnicate the wibble")
    assert wait_for(lambda: any(i["stage"] == "asking" for i in engine.indicators.values()))
    engine.abort()
    assert wait_for(lambda: (engine.history.get(res["id"]) or {}).get("status") in ("aborted", "unclear"))


def test_unclear_then_answer_continues(engine):
    engine.settings.set("agents.jeeves.functions", {"local_response": False})
    res = engine.handle_text("Jeeves, the pasta thing")
    assert wait_for(lambda: engine.waiters)
    engine.answer(None, text="set a timer for 4 minutes")
    assert wait_for(lambda: (engine.history.get(res["id"]) or {}).get("status") == "done")
    assert engine.history.get(res["id"])["function"] == "timers"


def test_run_command_confirmation_and_trust(engine):
    engine.settings.set("run_command.trusted", ["echo"])
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    entry = new_entry("x", "jeeves", "test")
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], entry)
    assert ctx.call("run_command", command="echo hello") == "hello"            # trusted: no confirm
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", ctx.call("run_command", command="printf yes")))
    t.start()
    assert wait_for(lambda: entry["id"] in engine.waiters)
    assert engine.indicator_click(entry["id"]) == "confirmed"                 # click the indicator
    t.join(5)
    assert out["r"] == "yes"
    from jeeves.functions.base import FunctionError
    with pytest.raises(FunctionError):
        ctx.call("run_command", command="echo hi; reboot")


def test_voice_keyword_confirms(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    entry = new_entry("x", "jeeves", "test")
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], entry)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("ok", ctx.confirm("Run: obs")))
    t.start()
    assert wait_for(lambda: engine.waiters)
    engine._deliver_answer("jeeves", "Proceed.")
    t.join(5)
    assert out["ok"] is True


def test_handoff_respects_permissions(engine):
    engine.settings.set("agents.claude", {"name": "Claude", "call_names": ["Claude"]})
    engine.settings.set("agents.jeeves.handoff_to", [])
    res = engine.handle_text("Jeeves, ask Claude to set a timer for 3 minutes", wait=True,
                             skip_functions=set())
    entry = engine.history.get(res["id"])
    assert entry["function"] == "handoff"
    assert entry["status"] == "error" and "isn't allowed" in entry["error"]
    engine.settings.set("agents.jeeves.handoff_to", ["claude"])
    res = engine.handle_text("Jeeves, ask Claude to set a timer for 3 minutes", wait=True)
    entry = engine.history.get(res["id"])
    assert entry["status"] == "done"
    assert any(t["kind"] == "handoff" for t in entry["trace"])
    assert any(h["agent"] == "claude" and h["function"] == "timers" for h in engine.history.list())


def test_extended_prompt_mode_collects_until_end_phrase(engine):
    engine.handle_text("Jeeves, extended prompt mode", wait=True)
    assert "jeeves" in engine.extended
    engine.handle_text("set a timer", "jeeves")
    engine.handle_text("for six minutes. End extended prompt mode", "jeeves")
    assert "jeeves" not in engine.extended
    assert wait_for(lambda: any(t["remaining"] > 300 for t in engine.timers.list()))


def test_ratings_become_dictionary_examples(engine):
    res = engine.handle_text("Jeeves, set a timer for 1 minute", wait=True)
    engine.history.rate(res["id"], -1, "I wanted a reminder", "local_response")
    ex = engine.history.rated_examples()
    assert ex["timers"][0]["good"] is False and ex["timers"][0]["should_use"] == "local_response"
    assert "local_response" in ex


def test_imported_functions_need_approval(engine):
    manifest = {"app": "afterglow", "functions": [
        {"name": "afterglow_clip", "description": "Clip the last 30 seconds", "keywords": ["clip that"],
         "steps": [{"call": "run_command", "args": {"command": "afterglow clip"}}]},
        {"name": "afterglow_bad", "description": "x", "steps": [{"call": "does_not_exist"}]}]}
    req = engine.imports.submit(manifest)
    assert req["items"][0]["default_functions_used"] == ["run_command"]
    assert req["items"][1]["problems"]
    assert engine.registry.get("afterglow_clip") is None              # nothing before approval
    res = engine.imports.decide(req["id"], approve=None)
    assert res["installed"] == ["afterglow_clip"]
    assert engine.registry.get("afterglow_clip").source == "app:afterglow"


def test_import_dropbox(engine):
    paths.imports_dir().mkdir(parents=True, exist_ok=True)
    (paths.imports_dir() / "obs.json").write_text(json.dumps({"app": "obs", "functions": [
        {"name": "obs_record", "description": "Start recording",
         "steps": [{"call": "run_command", "args": {"command": "obs --startrecording"}}]}]}))
    engine.imports.scan_dropbox()
    assert len(engine.imports.pending) == 1


def test_abort_releases_control_inputs(engine):
    released = []
    engine.control.release_all = lambda: released.append(True)
    engine.abort()
    assert released


def test_user_composition_runs_through_context(engine):
    engine.registry.save_user_function({
        "name": "greet_twice", "description": "Says hello twice", "keywords": ["greet me"],
        "args": [{"name": "who", "type": "string", "required": False, "default": "sir"}],
        "steps": [{"repeat": 2, "do": [{"call": "speak", "args": {"text": "Hello ${who}"}}]},
                  {"return": "greeted"}]})
    res = engine.handle_text("Jeeves, greet me", wait=True)
    entry = engine.history.get(res["id"])
    assert entry["function"] == "greet_twice"
    assert entry["response"] == "Hello sir\nHello sir"


def test_timer_schedule_runs_request(engine):
    import datetime as dt
    engine.timers.add_at(dt.datetime.now() + dt.timedelta(seconds=0.3), agent="jeeves",
                         request="set a timer for 9 minutes")
    assert wait_for(lambda: any(t["remaining"] > 500 for t in engine.timers.list()), 5)


def test_file_trigger_fires(engine, tmp_path):
    f = tmp_path / "watched.txt"
    f.write_text("a")
    fired = []
    engine.triggers.fire = lambda t: fired.append(t)
    engine.triggers.add({"event": "file_changed", "target": str(f)}, "jeeves", "set a timer for 1 minute")
    time.sleep(0.05)
    f.write_text("bb")
    import os
    os.utime(f, (time.time() + 5, time.time() + 5))
    engine.triggers._tick()
    assert fired and fired[0]["request"] == "set a timer for 1 minute"


def test_audio_keyword_trigger(engine):
    fired = []
    engine.triggers.fire = lambda t: fired.append(t)
    engine.triggers.add({"event": "audio_keyword", "target": "game over", "source": "desktop"}, "jeeves", "clip that")
    engine.triggers.on_transcript("and that's game over folks", "microphone")
    assert not fired
    engine.triggers.on_transcript("and that's game over folks", "desktop")
    assert fired


def test_summary_log_window(engine):
    engine.settings.set("summary.enabled", True)
    engine.summary.add("microphone", "hello there")
    engine.summary.add("desktop", "general kenobi")
    text = engine.summary.text(5)
    assert "[mic] hello there" in text and "[desktop] general kenobi" in text


# ---------------------------------------------------------------- puppetry
def test_puppetry_fire_and_save(engine, tmp_path):
    cfg = tmp_path / "macro-daemon"
    cfg.mkdir()
    engine.settings.set("puppetry.config_dir", str(cfg))
    (cfg / "state.json").write_text(json.dumps({"active_profile": "profile_2"}))
    received = []
    sock_path = str(cfg / "control.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        received.append(json.loads(conn.recv(4096).decode()))
        conn.sendall(b'{"ok": true}\n')
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    engine.puppetry.fire("Spam Click", ["10"])
    assert received == [{"cmd": "FIRE", "name": "Spam Click", "args": ["10"]}]
    srv.close()

    engine.puppetry.restart = lambda: None
    m = engine.puppetry.save_macro({**__import__("jeeves.daemon.puppetry", fromlist=["blank_macro"]).blank_macro(),
                                    "name": "Spam Click", "code": "tap(BTN_LEFT)"})
    data = json.loads((cfg / "macros.json").read_text())
    assert data["macros"][0]["name"] == "Spam Click"
    profile = json.loads((cfg / "profiles" / "profile_2.json").read_text())
    assert profile["enabled"][m["id"]] is True
    assert engine.puppetry.resolve_name("spam clik") == "Spam Click"
    ok, _ = engine.puppetry.check({"code": "tap(BTN_LEFT)\nfor i in range(3):\n    wait(0.1)"})
    assert ok
    ok, _ = engine.puppetry.check({"code": "for i in"})
    assert not ok


def test_off_switch_stops_everything(engine):
    unloaded = []
    engine.models.unload_all = lambda: unloaded.append(True)
    assert engine.toggle() is False
    assert unloaded
    assert "error" in engine.handle_text("Jeeves, set a timer for 2 minutes")
    assert engine.timers.list() == []
    with pytest.raises(ValueError):
        engine.voice_request("jeeves")
    from jeeves.models.manager import ModelUnavailable
    with pytest.raises(ModelUnavailable):
        engine.models.stt()
    assert engine.models.wake_spotter() is None
    engine.on_wake("microphone", "jeeves", 0.99, [])
    assert "microphone" not in engine.sessions
    assert not engine._needs_source("microphone")
    # dry runs still work (keyword matching, no models)
    assert engine.dry_run("Jeeves, set a timer for 2 minutes")["function"] == "timers"
    assert engine.toggle() is True
    engine.handle_text("Jeeves, set a timer for 2 minutes", wait=True)
    assert len(engine.timers.list()) == 1
