import pytest

from jeeves import config


def test_defaults_and_user_values():
    s = config.Settings()
    assert s.get("wake_word.global_threshold") == 0.6
    assert s.get("functions.online_agent") == "codex"
    s.set("wake_word.global_threshold", 0.8)
    assert s.get("wake_word.global_threshold") == 0.8
    assert config.Settings().get("wake_word.global_threshold") == 0.8   # persisted
    s.reset("wake_word.global_threshold")
    assert s.get("wake_word.global_threshold") == 0.6


def test_nixos_declared_keys_are_locked(system_settings):
    system_settings({"models": {"stt": {"model": "whisper-base-en"}}, "run_command": {"trusted": ["obs"]}})
    s = config.Settings()
    assert s.get("models.stt.model") == "whisper-base-en"
    assert s.is_locked("models.stt.model")
    assert s.is_locked("models.stt")            # ancestor of a locked key
    assert not s.is_locked("models.intent.model")
    with pytest.raises(config.LockedError):
        s.set("models.stt.model", "whisper-tiny-en")
    with pytest.raises(config.LockedError):
        s.set("run_command.trusted", [])
    s.set("models.intent.model", "qwen2.5-1.5b")   # unlocked neighbours still work
    assert s.get("models.intent.model") == "qwen2.5-1.5b"


def test_system_values_override_user_values(system_settings):
    s = config.Settings()
    s.set("summary.minutes", 30)
    system_settings({"summary": {"minutes": 90}})
    assert config.Settings().get("summary.minutes") == 90


def test_agents_get_defaults_and_default_agent_can_be_deleted():
    s = config.Settings()
    s.set("agents.claude", {"name": "Claude", "call_names": ["Claude"]})
    agents = s.get("agents")
    assert set(agents) == {"jeeves", "claude"}
    assert agents["claude"]["listen_to"] == "user"          # filled from default_agent()
    s.set("agents.jeeves", {"deleted": True})
    assert set(s.get("agents")) == {"claude"}


def test_secrets_are_private(jeeves_home):
    import os
    from jeeves import paths
    config.save_secret("gemini_api_key", "abc")
    assert config.get_secret("gemini_api_key") == "abc"
    assert os.stat(paths.secrets_file()).st_mode & 0o077 == 0
    assert "abc" not in paths.settings_file().read_text() if paths.settings_file().exists() else True
