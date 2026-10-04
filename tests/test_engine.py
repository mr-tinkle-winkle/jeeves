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


def test_suspend_and_close_a_request(engine):
    engine.settings.set("agents.jeeves.functions", {"local_response": False})
    res = engine.handle_text("Jeeves, the noodle thing")          # waits for an answer
    assert wait_for(lambda: engine.waiters)
    rid = res["id"]
    assert engine.suspend(rid) is True
    assert engine.indicators[rid]["suspended"] is True
    assert engine.suspend(rid) is False
    assert engine.close_request(rid)
    assert wait_for(lambda: (engine.history.get(rid) or {}).get("status") == "aborted")
    assert rid not in engine.indicators


def test_suspended_request_pauses_until_resumed(engine):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    entry = new_entry("x", "jeeves", "test")
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], entry)
    ctx.suspend_event.set()
    done = []
    t = threading.Thread(target=lambda: (ctx.check_cancelled(), done.append(True)))
    t.start()
    time.sleep(0.3)
    assert not done                       # blocked while suspended
    ctx.suspend_event.clear()
    t.join(2)
    assert done


def test_listening_session_suspend_and_close(engine):
    s = engine.open_session("microphone", "jeeves", "request")
    assert engine.suspend(s.request_id) is True and s.suspended
    assert engine.close_request(s.request_id)
    assert "microphone" not in engine.sessions and s.ended


def test_replacing_a_session_clears_its_indicator(engine):
    a = engine.open_session("microphone", "jeeves", "request")
    b = engine.open_session("microphone", None, "request")
    assert a.request_id not in engine.indicators and b.request_id in engine.indicators


def test_voice_request_unknown_needs_a_name(engine, monkeypatch):
    class STT:
        def __init__(self, text):
            self.text = text

        def transcribe(self, pcm, prompt="", language="en"):
            return self.text
    rid = engine.voice_request(None)
    s = engine.sessions["microphone"]
    assert s.agent_id is None and s.request_id == rid
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("Jeeves, set a timer for 6 minutes"))
    s.feed(b"\0" * 960, True, time.time())
    engine._finish_session(s)
    assert wait_for(lambda: engine.timers.list())
    notices = []
    engine._publish_fn = lambda topic, data: notices.append((topic, data))
    s2 = engine.open_session("microphone", None, "request")
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: STT("set a timer for 6 minutes"))
    s2.feed(b"\0" * 960, True, time.time())
    engine._finish_session(s2)
    assert any(t == "notice" and "agent's name" in d["text"] for t, d in notices)


def test_mic_only_listens_when_needed(engine):
    engine.settings.set("wake_word.enabled", False)
    assert not engine._needs_source("microphone")
    engine.open_session("microphone", None, "request")
    assert engine._needs_source("microphone")
    engine.settings.set("wake_word.enabled", True)


def test_hardware_recommendations():
    from jeeves.models import catalog, hardware
    cpu = {"ram_mb": 16000, "cpu_threads": 8, "gpus": [], "vram_mb": 0, "llama_gpu": [], "whisper_gpu": [],
           "gpu_usable": False}
    picks = hardware.recommend(cpu)["picks"]
    assert picks["wake"]["id"] == "vosk-small-en"
    assert catalog.get(picks["intent"]["id"]).speed >= 4
    resp = catalog.get(picks["local_response"]["id"])
    assert hardware.fit(resp, cpu) in ("cpu", "gpu")
    gpu = dict(cpu, ram_mb=64000, gpus=[{"name": "x", "vram_mb": 24000}], vram_mb=24000, llama_gpu=["vulkan"],
               whisper_gpu=["vulkan"], gpu_usable=True)
    gpicks = hardware.recommend(gpu)["picks"]
    assert catalog.get(gpicks["local_response"]["id"]).quality >= 5
    assert gpicks["stt"]["id"] == "whisper-large-v3-turbo"
    assert hardware.fit(catalog.get("llama3.3-70b"), cpu) == "no"
    assert all(m.speed and m.quality for m in catalog.CATALOG)


def _fake_gguf(path, size_mb, layers=36, emb=4096, heads=32, kv_heads=8):
    import struct

    def s(t):
        b = t.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [
        s("general.architecture") + struct.pack("<I", 8) + s("qwen3"),
        s("tokenizer.ggml.tokens") + struct.pack("<I", 9) + struct.pack("<IQ", 8, 3) + s("a") + s("b") + s("c"),
        s("tokenizer.ggml.scores") + struct.pack("<I", 9) + struct.pack("<IQ", 6, 3) + struct.pack("<3f", 1, 2, 3),
        s("qwen3.block_count") + struct.pack("<I", 4) + struct.pack("<I", layers),
        s("qwen3.embedding_length") + struct.pack("<I", 4) + struct.pack("<I", emb),
        s("qwen3.attention.head_count") + struct.pack("<I", 4) + struct.pack("<I", heads),
        s("qwen3.attention.head_count_kv") + struct.pack("<I", 4) + struct.pack("<I", kv_heads),
    ]
    with open(path, "wb") as f:
        f.write(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kv)) + b"".join(kv))
        f.truncate(int(size_mb * 1024 * 1024))


def test_gguf_metadata_and_auto_gpu_layers(tmp_path):
    from jeeves.models import gguf, hardware
    p = tmp_path / "model.gguf"
    _fake_gguf(p, 4000)
    meta = gguf.metadata(p)
    assert meta == {"arch": "qwen3", "block_count": 36, "embedding_length": 4096,
                    "attention.head_count": 32, "attention.head_count_kv": 8}
    hw = {"gpu_usable": True, "gpus": [{"vram_mb": 12000}], "vram_mb": 12000}
    n, info = hardware.auto_gpu_layers(p, 8192, hw, free_mb=10000)
    assert n == 99 and info["layers"] == 36                       # whole model fits
    n, info = hardware.auto_gpu_layers(p, 8192, hw, free_mb=3000)
    assert 0 < n < 36 and info["of"] == 36                        # part of it
    # per layer: ~108 MB weights + 32 MB KV cache (8192 ctx x 8 heads x 256 x 2 bytes)
    assert 130 < info["per_layer_mb"] < 150
    n, _ = hardware.auto_gpu_layers(p, 8192, hw, free_mb=500)
    assert n == 0                                                 # nothing fits
    n, info = hardware.auto_gpu_layers(p, 8192, {"gpu_usable": False, "gpus": [{"vram_mb": 8000}]})
    assert n == 0 and "backend" in info["why"]


def test_research_reads_pages_and_answers(engine, monkeypatch):
    from jeeves.functions.partials import web
    monkeypatch.setattr(web, "search", lambda settings, q, n: [
        {"title": "Site A", "url": "https://a.example", "snippet": "A says 42."},
        {"title": "Site B", "url": "https://b.example", "snippet": "B says 42 too."}])
    read = []

    def fake_site(ctx, url, raw=False, max_chars=20000):
        read.append(url)
        return f"The answer at {url} is 42. " * 20
    monkeypatch.setattr(web, "request_website", fake_site)
    prompts = []
    engine.models.respond = lambda agent, prompt, **kw: prompts.append(prompt) or "It's 42, according to Site A."
    res = engine.handle_text("Jeeves, look up the answer to everything", wait=True)
    entry = engine.history.get(res["id"])
    assert entry["function"] == "research" and entry["args"]["question"] == "the answer to everything"
    assert entry["response"] == "It's 42, according to Site A."
    assert read == ["https://a.example", "https://b.example"]
    assert "[1] Site A" in prompts[0] and "[2] Site B" in prompts[0]
    assert any(t["kind"] == "stage" and t["stage"] == "researching" for t in entry["trace"])


def test_agent_listening_to_a_specific_device(engine):
    engine.settings.set("agents.jeeves.listen_to", "device")
    engine.settings.set("agents.jeeves.listen_device", "alsa_output.usb-headset.monitor")
    agent = engine.agents()["jeeves"]
    assert engine._listens(agent, "device:alsa_output.usb-headset.monitor")
    assert not engine._listens(agent, "microphone") and not engine._listens(agent, "desktop")
    assert engine.call_names("device:alsa_output.usb-headset.monitor") == ["jeeves"]
    from jeeves.daemon import audio
    audio_which = audio.which
    try:
        audio.which = lambda *n: "/bin/" + n[0] if n[0] == "parec" else None
        assert "--device=alsa_output.usb-headset.monitor" in audio.capture_command(
            "alsa_output.usb-headset.monitor", "device")
    finally:
        audio.which = audio_which


def test_jump_in(engine, monkeypatch):
    from jeeves.daemon import jumpin
    assert jumpin.cooldown(1.0) < jumpin.cooldown(0.5) < jumpin.cooldown(0.1)
    assert jumpin.consider_chance(1.0) == 1.0 and jumpin.consider_chance(0.0) == 0.0
    engine.settings.set("agents.jeeves.listen_to", "both")
    assert not engine.jump_in.wants("desktop")
    engine.settings.set("agents.jeeves.jump_in", {"enabled": True, "frequency": 1.0})
    assert engine.jump_in.wants("desktop") and "transcribe" in engine.detection_modes("desktop")
    replies = iter(["PASS", "Ha, that's exactly what I said yesterday."])
    seen = []
    engine.models.respond = lambda agent, prompt, system="", **kw: seen.append((prompt, system)) or next(replies)
    engine.jump_in.heard("desktop", "did anyone see the match last night")
    engine.jump_in.consider("desktop")
    assert wait_for(lambda: not engine.jump_in.busy)
    assert engine.history.list() == []                       # PASS: stayed quiet
    assert "full participant" in seen[0][1] and "Others" in seen[0][0]
    engine.jump_in.heard("desktop", "it was the best game all season")
    engine.jump_in.consider("desktop")
    assert wait_for(lambda: engine.history.list())
    entry = engine.history.list()[0]
    assert entry["source"] == "jump_in" and entry["response"] == "Ha, that's exactly what I said yesterday."
    engine.jump_in.heard("desktop", "something else")
    engine.jump_in.consider("desktop")                       # cooldown: won't speak again straight away
    time.sleep(0.2)
    assert len(seen) == 2
    engine.settings.set("agents.jeeves.jump_in", {"enabled": True, "frequency": 0.0})
    assert not engine.jump_in.wants("desktop")


def test_prune_unloads_models_no_longer_chosen(engine):
    class Inst:
        def __init__(self):
            self.unloaded = False

        def unload(self):
            self.unloaded = True

        def loaded(self):
            return not self.unloaded
    old, cur = Inst(), Inst()
    engine.settings.set("models.intent.model", "qwen3-1.7b")
    engine.models.instances = {"qwen2.5-1.5b": old, "qwen3-1.7b": cur}
    assert engine.models.prune() == ["qwen2.5-1.5b"]
    assert old.unloaded and not cur.unloaded


def test_virtual_mic_carries_your_mic(monkeypatch):
    from jeeves.daemon import audio
    calls = []
    modules = {"text": ""}

    def fake_pactl(*args):
        calls.append(args)
        if args[:3] == ("list", "short", "modules"):
            return modules["text"]
        if args == ("get-default-source",):
            return "alsa_input.usb-mic\n"
        return ""
    monkeypatch.setattr(audio, "_pactl", fake_pactl)
    monkeypatch.setattr(audio, "which", lambda *n: "/bin/pactl")
    assert audio.ensure_virtual_mic("jeeves-mic")
    loads = [c for c in calls if c[0] == "load-module"]
    assert [c[1] for c in loads] == ["module-null-sink", "module-remap-source", "module-loopback"]
    assert "source=alsa_input.usb-mic" in loads[2] and "sink=jeeves-mic" in loads[2]
    # already there: nothing loaded twice
    modules["text"] = ("1\tmodule-null-sink\tsink_name=jeeves-mic x\n2\tmodule-remap-source\tsource_name=jeeves-mic-source\n"
                       "3\tmodule-loopback\tsource=alsa_input.usb-mic sink=jeeves-mic latency_msec=20\n")
    calls.clear()
    audio.ensure_virtual_mic("jeeves-mic")
    assert not [c for c in calls if c[0] == "load-module"]
    # the default source being Jeeves' own mic must not make it listen to itself
    monkeypatch.setattr(audio, "_pactl", lambda *a: "jeeves-mic-source\n" if a == ("get-default-source",) else
                        "1\tjeeves-mic-source\tx\n2\talsa_input.real\tx\n" if a[:2] == ("list", "short") else "")
    assert audio.real_mic_source() == "alsa_input.real"
