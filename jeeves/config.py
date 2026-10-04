"""Settings: defaults, the user's saved values, and the NixOS-declared values
that lock keys.

Effective settings = DEFAULTS <- user settings.json <- /etc/jeeves/settings.json.
Any leaf that the system file declares is *locked*: ``set()`` refuses to
change it and the GUI draws it disabled with a lock.

Keys are addressed with dotted paths ("models.stt.model",
"agents.jeeves.prompt"). Lists are leaves -- locking a list locks all of it.

Reads are cached by file mtime (the GUI's theme provider calls this from
paint events -- UI_THEMING_GUIDE.md pitfall 1).
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable

from . import paths

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

INDICATOR_COLORS = {
    "thinking": "#808080",
    "researching": "#2f6fff",
    "responding": "#000000",
    "responding_outline": "#ffffff",
    "asking": "#ffffff",
    "unclear": "#8a2be2",
    "unavailable": "#ff2020",
}


def default_agent(name: str, call_names: list[str] | None = None, prompt: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "enabled": True,
        "call_names": call_names or [name],
        # None -> use wake_word.global_threshold (when the global option is on)
        "threshold": None,
        "listen_to": "user",          # user | desktop | both
        "output_to": "speakers",      # speakers | microphone | both
        "show_output": True,          # also show the response text on screen
        "enable_when_open": [],       # agent only active while one of these apps is open
        "enable_when_focused": [],    # ... or focused
        "disable_when_open": [],
        "disable_when_focused": [],
        # None -> global model choice
        "models": {"stt": None, "intent": None, "tts": None, "tts_voice": None, "local_response": None},
        "prompt": prompt,
        # function name -> bool; missing -> the function's own default
        "functions": {},
        # agent ids this agent may hand off to (Handoff function); "*" = any agent
        "handoff_to": ["*"],
        "indicator_color": None,      # tint of the processing spinner; None -> state colors only
    }


DEFAULTS: dict[str, Any] = {
    "general": {
        "enabled": True,                  # master switch: off = no listening, no models, no requests
        "end_of_speech_seconds": 1.2,     # silence before listening stops
        "mic_click_extend_seconds": 5.0,  # clicking the onscreen mic adds this
        "mic_hold_release_seconds": 1.0,  # after letting go of a held mic
        "max_request_seconds": 60.0,
        "abort_key": "KEY_PAUSE",
        "watch_keyboard_for_keybinds": True,  # evdev, read-only, for keybinds + abort
        "history_limit": 500,
        "unclear_confidence": 0.45,       # below this the intent is "unclear"
        "notifications": True,
    },
    "wake_word": {
        # If summary.enabled is on, wake word is off: agent names are found in
        # the continuous transcript instead.
        "enabled": True,
        "engine": "vosk",                 # vosk | stt-match
        "model": None,                    # catalog id; None until downloaded
        "global_threshold_enabled": True,
        "global_threshold": 0.6,
    },
    "models": {
        # all "model" values are catalog ids from jeeves.models.catalog
        "stt": {"model": None, "unload_when_open": [], "language": "en"},
        "intent": {"model": None, "unload_when_open": []},
        "tts": {"model": "espeak-ng", "unload_when_open": []},
        "tts_voice": "espeak-en",
        "local_response": {"model": None, "unload_when_open": [], "max_tokens": 512},
        "gpu_layers": 0,                  # llama.cpp -ngl
        "favorites": [],                  # starred models (shown first on the Models page)
        "reasoning": "off",               # thinking models: off (fast) | auto (think first, smarter)
        "threads": 0,                     # 0 = auto
    },
    "audio": {
        "microphone": "",                 # PipeWire/Pulse source; "" = default
        "desktop": "@DEFAULT_MONITOR@",
        "speaker": "",                    # sink; "" = default
        "virtual_mic_sink": "jeeves-mic", # null sink that apps can record from (output_to=microphone)
        "vad_threshold": 0.012,           # RMS level that counts as speech
    },
    "agents": {
        "jeeves": default_agent("Jeeves", prompt="You are Jeeves, a dry-witted, efficient butler."),
    },
    "accounts": {
        "codex": {"enabled": False, "binary": "codex"},
        "gemini": {"enabled": False, "model": "gemini-2.5-flash"},
        "claude": {"enabled": False},
        "grok": {"enabled": False},
        "browser": {"binary": "", "clipboard_timeout_seconds": 300},
    },
    "indicators": {
        "corner": "top-right",            # top-left | top-right | bottom-left | bottom-right
        "screen": "mouse",                # mouse (the monitor the mouse is on) | primary | an output name
        "stt_active": True,
        "stt_output": True,
        "processing": True,
        "timers": True,                   # bottom right, per SPEC
        "response_text": True,
        "size": 56,
        "colors": dict(INDICATOR_COLORS),
    },
    "manual_request": {
        "text_keybind_enabled": True,
        "text_keybind": ["KEY_LEFTMETA", "KEY_J"],
        "voice_keybind_enabled": True,
        # agent id -> combo; "_unknown" = Voice Request: Unknown (say the agent's name)
        "voice_keybinds": {"jeeves": ["KEY_LEFTMETA", "KEY_LEFTSHIFT", "KEY_J"], "_unknown": []},
        "review_keybind": ["KEY_LEFTMETA", "KEY_LEFTSHIFT", "KEY_R"],
        "toggle_keybind": [],             # turn Jeeves on/off (same as `jeeves --toggle`)
    },
    "run_command": {
        "confirm": True,
        "confirm_keyword": "proceed",
        "trusted": ["puppetry --list"],
        "timeout_seconds": 30,
    },
    "functions": {
        "keywords": {},                   # function -> [keywords]; overrides defaults
        "blocked": {},                    # function -> [blocked statements]
        "global_enabled": {},             # function -> bool (master switch)
        "online_agent": "codex",          # codex | gemini | claude | grok
    },
    "memory": {"recent_count": 3, "long_term_limit": 200},
    "summary": {"enabled": False, "minutes": 60, "sources": ["microphone", "desktop"]},
    "control_mode": {"virtual_controller": False, "absolute_moves": True},
    "puppetry": {"config_dir": "~/.config/macro-daemon", "service": "macro-daemon.service"},
    "wikipedia": {"variant": "wikipedia_en_all_nopic", "mirror": "https://download.kiwix.org/zim/wikipedia/",
                  "file": None, "smart_update": True},
    "training": {"stt_vocabulary": [], "intent_examples_from_ratings": True, "max_examples": 12},
    "triggers": [],                       # see jeeves.daemon.triggers
    "theme": None,                        # filled from ThemeSettings() by the GUI on first save
}

# ---------------------------------------------------------------------------
# Dotted-path helpers
# ---------------------------------------------------------------------------


def get_path(tree: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = tree
    for part in path.split(".") if path else []:
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(tree: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def delete_path(tree: dict[str, Any], path: str) -> None:
    parts = path.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.get(part)
        if not isinstance(node, dict):
            return
    node.pop(parts[-1], None)


def leaf_paths(tree: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(tree, dict) and tree:
        for k, v in tree.items():
            yield from leaf_paths(v, f"{prefix}.{k}" if prefix else str(k))
    elif prefix:
        yield prefix


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _fill_agent_defaults(settings: dict[str, Any]) -> None:
    agents = settings.get("agents") or {}
    for aid, agent in list(agents.items()):
        if not isinstance(agent, dict) or agent.get("deleted"):
            # default agents can't be removed from DEFAULTS, so deleting one
            # stores {"deleted": true} in the user's file
            agents.pop(aid, None)
        else:
            agents[aid] = deep_merge(default_agent(agent.get("name", aid)), agent)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class LockedError(PermissionError):
    pass


class _FileCache:
    def __init__(self, path_fn):
        self._path_fn = path_fn
        self._key = None
        self._data: dict[str, Any] = {}

    def read(self) -> dict[str, Any]:
        path: Path = self._path_fn()
        try:
            st = path.stat()
        except OSError:
            self._key, self._data = None, {}
            return self._data
        key = (str(path), st.st_mtime_ns, st.st_size)
        if key != self._key:
            try:
                self._data = json.loads(path.read_text() or "{}")
            except (OSError, ValueError):
                self._data = {}
            self._key = key
        return self._data


class Settings:
    """Thread-safe settings store. The daemon owns the only writable instance;
    the GUI reads through the daemon (and may read the files directly for the
    theme provider)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._user = _FileCache(paths.settings_file)
        self._system = _FileCache(paths.system_settings_file)
        self._effective_key = None
        self._effective: dict[str, Any] = {}

    # ---- reading ---------------------------------------------------------
    def user(self) -> dict[str, Any]:
        return copy.deepcopy(self._user.read())

    def system(self) -> dict[str, Any]:
        return copy.deepcopy(self._system.read())

    def locked_paths(self) -> set[str]:
        return set(leaf_paths(self._system.read()))

    def is_locked(self, path: str) -> bool:
        locked = self.locked_paths()
        # a key is locked if it, an ancestor, or a descendant is declared
        return any(path == p or path.startswith(p + ".") or p.startswith(path + ".") for p in locked)

    def effective_readonly(self) -> dict[str, Any]:
        with self._lock:
            u, s = self._user.read(), self._system.read()
            key = (id(u), self._user._key, id(s), self._system._key)
            if key != self._effective_key:
                eff = deep_merge(DEFAULTS, u)
                eff = deep_merge(eff, s)
                _fill_agent_defaults(eff)
                self._effective, self._effective_key = eff, key
            return self._effective

    def effective(self) -> dict[str, Any]:
        return copy.deepcopy(self.effective_readonly())

    def get(self, path: str, default: Any = None) -> Any:
        return copy.deepcopy(get_path(self.effective_readonly(), path, default))

    # ---- writing ---------------------------------------------------------
    def set(self, path: str, value: Any) -> None:
        self.set_many({path: value})

    def set_many(self, changes: dict[str, Any]) -> None:
        with self._lock:
            for p in changes:
                if self.is_locked(p):
                    raise LockedError(f"'{p}' is declared in NixOS and can't be changed here")
            data = self.user()
            for p, v in changes.items():
                set_path(data, p, v)
            self._write(data)

    def delete(self, path: str) -> None:
        with self._lock:
            if self.is_locked(path):
                raise LockedError(f"'{path}' is declared in NixOS and can't be changed here")
            data = self.user()
            delete_path(data, path)
            self._write(data)

    def reset(self, path: str) -> None:
        """Back to the default (removes the user's value)."""
        self.delete(path)

    def _write(self, data: dict[str, Any]) -> None:
        path = paths.settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Secrets (API keys)
# ---------------------------------------------------------------------------


def load_secrets() -> dict[str, Any]:
    try:
        return json.loads(paths.secrets_file().read_text())
    except (OSError, ValueError):
        return {}


def save_secret(name: str, value: str | None) -> None:
    data = load_secrets()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    path = paths.secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def get_secret(name: str) -> str | None:
    env = os.environ.get(f"JEEVES_{name.upper()}")
    if env:
        return env
    file_env = os.environ.get(f"JEEVES_{name.upper()}_FILE")
    if file_env:
        try:
            return Path(file_env).read_text().strip()
        except OSError:
            pass
    return load_secrets().get(name)
