import json
import textwrap

import pytest

from jeeves import paths
from jeeves.config import Settings
from jeeves.functions.base import FunctionError
from jeeves.functions.composer import evaluate, interpolate, partials_used, run_steps, validate
from jeeves.functions.registry import Registry
from jeeves.functions.safety import UnsafeCommand, is_trusted, split


# ---------------------------------------------------------------- safety
@pytest.mark.parametrize("cmd", ["ls; rm -rf ~", "echo hi && reboot", "cat x | sh", "echo $(whoami)", "echo `id`",
                                 "echo hi > file", "sleep 1 &", "a || b", "echo ${HOME}"])
def test_shell_metacharacters_rejected(cmd):
    with pytest.raises(UnsafeCommand):
        split(cmd)


def test_split_and_trusted_list():
    assert split("puppetry --list") == ["puppetry", "--list"]
    assert split("notify-send 'two words'") == ["notify-send", "two words"]
    trusted = ["puppetry --list", "obs"]
    assert is_trusted(["puppetry", "--list"], trusted)
    assert not is_trusted(["puppetry", "--name=x"], trusted)     # arguments listed: exactly those
    assert is_trusted(["obs", "--startrecording"], trusted)       # no arguments listed: any
    assert is_trusted(["/usr/bin/obs"], trusted)
    assert not is_trusted(["rm", "-rf"], trusted)


# ---------------------------------------------------------------- composer
class FakeCtx:
    def __init__(self):
        self.calls = []
        self.triggers = []

    def call(self, name, **args):
        self.calls.append((name, args))
        if name == "get_open_apps":
            return ["firefox", "obs"]
        if name == "add":
            return args["a"] + args["b"]
        return f"{name} done"

    def check_cancelled(self):
        pass

    def wait(self, s):
        self.calls.append(("wait", s))

    def register_trigger(self, spec, steps, variables):
        self.triggers.append(spec)
        return "t1"


def test_interpolation_keeps_types():
    v = {"n": 3, "app": {"name": "obs"}, "items": [1, 2]}
    assert interpolate("${n}", v) == 3
    assert interpolate("app is ${app.name}", v) == "app is obs"
    assert interpolate({"x": ["${items.1}"]}, v) == {"x": [2]}


def test_conditions_are_sandboxed():
    v = {"apps": ["obs"], "count": 2}
    assert evaluate('"obs" in apps and count >= 2', v) is True
    assert evaluate("len(apps) == 1", v) is True
    assert evaluate("contains(${apps.0}, 'OB')", v) is True
    for bad in ("__import__('os')", "apps.__class__", "open('/etc/passwd')", "(lambda: 1)()"):
        with pytest.raises(FunctionError):
            evaluate(bad, v)


def test_run_steps_control_flow():
    ctx = FakeCtx()
    steps = [
        {"call": "get_open_apps", "as": "apps"},
        {"if": '"obs" in apps', "then": [{"call": "speak", "args": {"text": "OBS is open"}}],
         "else": [{"call": "run_command", "args": {"command": "obs"}}]},
        {"set": "total", "value": 0},
        {"repeat": 3, "as": "i", "do": [{"set": "total", "expr": "total + i"}]},
        {"for_each": "${apps}", "as": "a", "do": [{"call": "speak", "args": {"text": "${a}"}}]},
        {"set": "n", "value": 0},
        {"while": "n < 2", "do": [{"set": "n", "expr": "n + 1"}]},
        {"wait": "2s"},
        {"when": {"event": "app_focused", "target": "steam"}, "do": [{"call": "speak", "args": {"text": "hi"}}]},
        {"return": "${total}-${n}"},
        {"call": "never"},
    ]
    assert run_steps(steps, ctx, {}) == "3-2"
    names = [c[0] for c in ctx.calls]
    assert names == ["get_open_apps", "speak", "speak", "speak", "wait"]
    assert ctx.calls[1][1] == {"text": "OBS is open"}
    assert ctx.triggers == [{"event": "app_focused", "target": "steam"}]
    assert partials_used(steps) >= {"get_open_apps", "speak", "run_command", "never", "event_trigger", "wait"}


def test_while_loop_is_bounded():
    with pytest.raises(FunctionError):
        run_steps([{"while": "true", "max": 5, "do": []}], FakeCtx(), {})


def test_validate_reports_problems():
    assert validate([{"call": "speak"}], {"speak"}) == []
    probs = validate([{"call": "nope"}, {"bogus": 1}, {"if": "a ==", "then": []}], {"speak"})
    assert len(probs) == 3


# ---------------------------------------------------------------- registry
def test_builtins_registered_with_spec_defaults():
    r = Registry(Settings())
    fulls = {f.name: f for f in r.all("full")}
    assert set(fulls) == {"summary", "extended_prompt_mode", "online_prompt", "control_mode", "local_response",
                          "macros", "timers", "handoff"}
    on = {n for n, f in fulls.items() if f.default_enabled}
    assert on == {"extended_prompt_mode", "local_response", "timers", "handoff"}   # SPEC defaults
    partials = {f.name for f in r.all("partial")}
    for needed in ("run_command", "find_on_screen", "read_screen_text", "get_mouse_position", "get_held_keys",
                   "get_focused_app", "get_open_apps", "get_app_position", "get_app_workspace", "get_app_size",
                   "recent_requests", "remember", "mark_screen_position", "get_clipboard", "set_clipboard",
                   "control_flow", "event_trigger", "play_sound", "speak", "ask_user", "wait", "send_notification",
                   "request_website", "read_file", "create_file", "run_file"):
        assert needed in partials
    assert r.problems == []


def test_keyword_and_blocked_overrides_and_dictionary_text():
    s = Settings()
    s.set("functions.keywords.timers", ["countdown"])
    s.set("functions.blocked.timers", ["cooking advice"])
    r = Registry(s)
    t = r.get("timers")
    assert r.keywords(t) == ["countdown"]
    text = r.dictionary_text([t], {"timers": [{"text": "ten minute timer", "good": True}]})
    assert "countdown" in text and "cooking advice" in text and "one of ['timer'" in text
    assert "Rated GOOD" in text


def test_user_function_save_export_and_delete():
    r = Registry(Settings())
    f = r.save_user_function({"name": "open_obs", "description": "Opens OBS", "keywords": ["open obs"],
                              "steps": [{"call": "run_command", "args": {"command": "obs"}}]})
    assert f.source == "user" and f.uses == ["run_command"]
    assert (paths.functions_dir() / "open_obs.json").exists()
    manifest = r.export()
    assert manifest["functions"][0]["name"] == "open_obs"
    with pytest.raises(ValueError):
        r.save_user_function({"name": "bad", "description": "x", "steps": [{"call": "missing_partial"}]})
    with pytest.raises(ValueError):
        r.save_user_function({"name": "timers", "description": "x", "steps": []})   # builtin name
    r.delete_user_function("open_obs")
    assert r.get("open_obs") is None


def test_user_partials_load_from_python_files():
    paths.partials_dir().mkdir(parents=True, exist_ok=True)
    (paths.partials_dir() / "weather.py").write_text(textwrap.dedent('''
        from jeeves.functions import partial, Arg

        @partial("get_weather", "Gets the weather", args=[Arg("city", "string", "City")])
        def get_weather(ctx, city):
            return f"sunny in {city}"
    '''))
    (paths.partials_dir() / "broken.py").write_text("this is not python")
    r = Registry(Settings())
    f = r.get("get_weather")
    assert f is not None and f.source == "user"
    assert f.impl(None, city="Paris") == "sunny in Paris"
    assert any("broken.py" in p for p in r.problems)


def test_agent_function_toggles():
    r = Registry(Settings())
    agent = {"functions": {"macros": True, "timers": False}}
    names = {f.name for f in r.enabled_for(agent)}
    assert "macros" in names and "timers" not in names and "local_response" in names


def test_json_function_files_from_apps_are_loaded():
    d = paths.functions_dir() / "apps" / "afterglow"
    d.mkdir(parents=True)
    (d / "clip.json").write_text(json.dumps({"name": "afterglow_clip", "description": "Clip",
                                              "steps": [{"call": "run_command", "args": {"command": "afterglow clip"}}]}))
    r = Registry(Settings())
    assert r.get("afterglow_clip").source == "app:afterglow"
