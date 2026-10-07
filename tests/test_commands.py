"""An agent's own commands (Agents > Commands)."""
import sys

import pytest

from jeeves.functions.base import FunctionError
from jeeves.functions.commands import argv_for, command_defs


def test_placeholders_become_single_arguments_never_shell():
    argv = argv_for("nixos-rebuild switch --flake ~/nix#{host}", {"host": "desk; rm -rf /"})
    assert argv[:3] == ["nixos-rebuild", "switch", "--flake"] and argv[3].endswith("/nix#desk; rm -rf /")
    assert argv_for("git commit -m {message} {extra}", {"message": "fix the bar"}) == \
        ["git", "commit", "-m", "fix the bar"]                          # an optional argument not given: left out


def test_commands_are_functions_for_that_agent_only(engine):
    engine.settings.set("agents.jeeves.commands", [
        {"name": "rebuild", "description": "Rebuilds my NixOS system", "command": "nixos-rebuild switch --flake .#{host}",
         "args": [{"name": "host", "description": "which computer", "choices": ["desk", "laptop"]}]},
        {"name": "timers", "description": "clashes with a built-in", "command": "echo hi"}])
    names = [f.name for f in engine.registry.enabled_for(engine.agents()["jeeves"])]
    assert "rebuild" in names and "my_timers" in names
    f = engine.registry.get("rebuild", engine.agents()["jeeves"])
    assert f.args[0].name == "host" and f.args[0].choices == ["desk", "laptop"]
    assert "Rebuilds my NixOS system" in engine.registry.dictionary_text([f], brief=True)
    assert engine.registry.get("rebuild") is None                     # not a global function


def test_running_a_command_with_the_arguments_said(engine, monkeypatch):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    engine.settings.set("agents.jeeves.commands", [
        {"name": "say-back", "description": "Prints a word", "command": f"{sys.executable} -c 'import sys; print(sys.argv[1])' {{word}}",
         "args": [{"name": "word", "description": "the word", "required": True}], "confirm": False}])
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("say back hello", "jeeves", "text"))
    ctx.call("say_back", word="hello")
    assert said == ["say-back finished. hello"]
    engine.settings.set("agents.jeeves.commands", [
        {"name": "fail", "command": f"{sys.executable} -c 'import sys; sys.exit(\"broken thing\")'", "confirm": False}])
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("fail", "jeeves", "text"))
    with pytest.raises(FunctionError, match="broken thing"):
        ctx.call("fail")


def test_bad_command_specs_are_skipped():
    assert command_defs({"commands": [{"name": "x"}, "junk", {"command": "ls"}]}, set()) == []
