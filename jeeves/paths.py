"""Where Jeeves keeps things. Every path honours the XDG variables and can be
redirected wholesale with ``JEEVES_HOME`` (used by the tests)."""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    home = os.environ.get("JEEVES_HOME")
    if home:
        return Path(home) / fallback.strip(".").replace("/", "_")
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def config_dir() -> Path:
    """User settings, custom functions, custom partials."""
    return _xdg("XDG_CONFIG_HOME", ".config") / "jeeves"


def data_dir() -> Path:
    """Models, Wikipedia, history, permanent memory, training data."""
    return _xdg("XDG_DATA_HOME", ".local/share") / "jeeves"


def state_dir() -> Path:
    """Logs (summary transcript, daemon log)."""
    return _xdg("XDG_STATE_HOME", ".local/state") / "jeeves"


def runtime_dir() -> Path:
    home = os.environ.get("JEEVES_HOME")
    if home:
        return Path(home) / "run"
    value = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(value) if value else Path("/tmp") / f"jeeves-{os.getuid()}") / "jeeves"


def system_settings_file() -> Path:
    """Settings declared in NixOS (services.jeeves.settings). Read-only, and
    every key in it is shown as locked in the GUI."""
    return Path(os.environ.get("JEEVES_SYSTEM_SETTINGS", "/etc/jeeves/settings.json"))


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def secrets_file() -> Path:
    """API keys (Gemini). Kept out of settings.json so exporting or sharing
    settings never leaks them. Mode 0600."""
    return config_dir() / "secrets.json"


def functions_dir() -> Path:
    """User-made full functions (one JSON file each)."""
    return config_dir() / "functions"


def partials_dir() -> Path:
    """User-made partial functions (Python files)."""
    return config_dir() / "partials"


def imports_dir() -> Path:
    """Drop-box where other apps place function manifests for approval."""
    return data_dir() / "imports"


def models_dir() -> Path:
    return data_dir() / "models"


def ensure(*dirs: Path) -> None:
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def ensure_all() -> None:
    ensure(config_dir(), data_dir(), state_dir(), runtime_dir(), functions_dir(), partials_dir(),
           imports_dir(), models_dir())
    try:
        os.chmod(runtime_dir(), 0o700)
    except OSError:
        pass
