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
        "listen_to": "user",          # user | desktop | both | device
        "listen_device": "",          # listen_to=device: exact source (a mic, or an output's .monitor)
        # said when it starts listening and you're waiting ("Yes?"); several separated by | (one is picked
        # at random); "" = say nothing
        "wake_reply": "",
        # its own commands (Agents > Commands): [{name, description, command with {placeholders}, args:
        # [{name, description, required, choices}], confirm, terminal, timeout}]
        "commands": [],
        "jeenius": None,              # 1-4 (see models.jeenius); None -> the global setting
        # Jump in whenever the AI wants to: 1 = a full part of the conversation, 0 = never
        "jump_in": {"enabled": False, "frequency": 0.3},
        "output_to": "speakers",      # speakers | microphone | both | device | device_mic
        "output_device": "",          # output_to=device/device_mic: exact output (sink), e.g. a headset
        "show_output": True,          # also show the response text on screen
        "enable_when_open": [],       # agent only active while one of these apps is open
        "enable_when_focused": [],    # ... or focused
        "disable_when_open": [],
        "disable_when_focused": [],
        # None -> global model choice
        "models": {"stt": None, "intent": None, "tts": None, "tts_voice": None, "local_response": None},
        "prompt": prompt,
        "persona_check": False,       # grade each reply against the prompt and rewrite it once if it's off
        # function name -> bool; missing -> the function's own default
        "functions": {},
        # agent ids this agent may hand off to (Handoff function); "*" = any agent
        "handoff_to": ["*"],
        "indicator_color": None,      # tint of the processing spinner; None -> state colors only
        # memory: recent = past requests shown to the model (None -> memory.recent_count),
        # notes = remembered notes shown to it, own_only = only this agent's requests/notes
        "memory": {"enabled": True, "recent": None, "notes": 30, "own_only": False},
        # how the voice is shaped (models.tts_voice picks the voice itself)
        "voice_style": {"speaker": "", "speed": 1.0, "pitch": 0.0, "expressiveness": 0.667, "effect": "none",
                        "blend": "", "blend_amount": 0.3},
    }


def agent_memory(agent: dict[str, Any] | None, settings: Any = None) -> dict[str, Any]:
    """The agent's effective memory settings (older agents have none saved)."""
    m = dict((agent or {}).get("memory") or {})
    default_recent = int(settings.get("memory.recent_count", 3)) if settings is not None else 3
    enabled = bool(m.get("enabled", True))
    recent = m.get("recent")
    return {"enabled": enabled,
            "recent": max(0, int(default_recent if recent is None else recent)) if enabled else 0,
            "notes": max(0, int(m.get("notes", 30) if m.get("notes") is not None else 30)) if enabled else 0,
            "own_only": bool(m.get("own_only", False))}


DEFAULTS: dict[str, Any] = {
    "general": {
        "enabled": True,                  # master switch: off = no listening, no models, no requests
        "end_of_speech_seconds": 1.2,     # silence before listening stops
        "mic_click_extend_seconds": 5.0,  # clicking the onscreen mic adds this
        "mic_hold_release_seconds": 1.0,  # after letting go of a held mic
        "max_request_seconds": 30.0,
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
        "verify_near_misses": True,       # unsure detections (half the threshold or more) are checked by STT
        # things you say that the wake word model heard no name in are checked by speech recognition too,
        # which catches the calls it misses ("hey Jeeves what time is it", fast, over loud sound)
        "stt_backup": True,
    },
    "models": {
        # all "model" values are catalog ids from jeeves.models.catalog
        "stt": {"model": None, "unload_when_open": [], "language": "en"},
        "intent": {"model": None, "unload_when_open": []},
        "tts": {"model": "espeak-ng", "unload_when_open": []},
        "tts_voice": "espeak-en",
        "local_response": {"model": None, "unload_when_open": [], "max_tokens": 512},
        "vision": {"model": None, "unload_when_open": []},   # a model that can see (screen watching)
        "gpu_layers": "auto",             # llama.cpp -ngl: "auto" (fit free VRAM), 0 = CPU only, 99 = all
        "favorites": [],                  # starred models (shown first on the Models page)
        "reasoning": "off",               # (old setting, replaced by jeenius)
        # the Jeenius scale (thinking models): 1 instant, 2 thinks when needed, 3 instant when it can,
        # 4 always thinks. Agents can have their own (agents.<id>.jeenius).
        "jeenius": 2,
        "threads": 0,                     # 0 = auto
        # Minimum untouched: what the AIs must always leave for everything else. Limits what
        # gets loaded (and how) and what the Models page recommends.
        "keep_free": {"ram_gb": 4.0, "vram_gb": 1.0, "cpu_threads": 1, "gpu_percent": 0},
        # Hybrid Models: a lighter set while other programs need the computer (jeeves.models.hybrid).
        # Per kind: "auto" (a smaller downloaded model), "same", "off" (vision only) or a model id.
        "hybrid": {"enabled": True, "stt": "auto", "intent": "auto", "local_response": "auto", "vision": "off",
                   "cpu_percent": 60, "gpu_percent": 50, "ram_free_gb": 3.0, "switch_after": 8, "back_after": 45},
    },
    "audio": {
        "microphone": "",                 # PipeWire/Pulse source; "" = default
        "desktop": "@DEFAULT_MONITOR@",
        "speaker": "",                    # sink; "" = default
        "virtual_mic_sink": "jeeves-mic", # null sink that apps can record from (output_to=microphone)
        "virtual_mic_include_mic": True,  # Jeeves-Microphone carries your real mic too (pick it in Discord)
        "vad_threshold": 0.012,           # RMS level that counts as speech (level meter only)
        # what tells speech from other sound: "auto" = the neural speech detector (Silero VAD; hears you
        # over music, games and key presses), "level" = the level meter (louder than the background)
        "speech_detector": "auto",
    },
    "agents": {
        "jeeves": default_agent("Jeeves", prompt=(
            "You are Jeeves, an unflappable British butler. Formal, concise and quietly amused; you call the user "
            "'sir', favour understatement and dry wit, and never gush or sound like a generic chatbot.")),
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
    "research": {"engine": "duckduckgo", "searxng_url": "", "pages": 3, "max_chars_per_page": 4000,
                 "depth": "normal",            # quick (1 round, 3 pages) | normal (2 rounds, 5) | deep (3, 8)
                 "auto_for_facts": True,       # factual questions (games, products, people...) get researched
                 "max_seconds": 90},           # stop reading more pages after this long
    # YouTube asks some connections to sign in; yt-dlp can use a browser's YouTube login on this computer:
    # "auto" (only when asked, trying each installed browser), "off", or a browser: firefox, chrome, chromium,
    # brave, vivaldi, edge, librewolf (optionally "firefox:PROFILE"); cookies_file = an exported cookies.txt
    "youtube": {"max_height": 1080, "cookies_from_browser": "auto", "cookies_file": ""},
    "summary": {"enabled": False, "minutes": 60, "sources": ["microphone", "desktop"]},
    # Watch the screen: seconds between looks (backs off while nothing changes), how chatty, time limit
    "watch": {"interval": 2.0, "talkativeness": 0.5, "max_minutes": 120},
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
