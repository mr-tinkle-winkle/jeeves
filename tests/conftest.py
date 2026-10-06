import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def jeeves_home(tmp_path, monkeypatch):
    """Every test gets its own config/data/runtime folders and no system file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JEEVES_HOME", str(home))
    monkeypatch.setenv("JEEVES_SYSTEM_SETTINGS", str(tmp_path / "system-settings.json"))
    monkeypatch.setenv("JEEVES_NO_OVERLAY", "1")
    return home


@pytest.fixture
def system_settings(tmp_path):
    def write(data):
        (tmp_path / "system-settings.json").write_text(json.dumps(data))
    return write


@pytest.fixture
def engine():
    from jeeves.daemon.engine import Engine
    e = Engine(start_io=False)
    # the tests' audio is synthetic tones, which the neural speech detector rightly doesn't take for
    # speech: judge it with the level meter (tests/test_vad.py covers the detector itself)
    e.settings.set("audio.speech_detector", "level")
    yield e
    e.timers.stop()
    e.pool.shutdown(wait=False, cancel_futures=True)


def pytest_configure(config):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
