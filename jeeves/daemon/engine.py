"""The Jeeves engine: ties settings, models, listening, the Dictionary and the
request pipeline together. The socket server (server.py) is a thin layer
over this object.

Request pipeline (SPEC "Pipeline"):
  wake word / manual request -> STT -> intent (agent's enabled functions)
  -> function runs (composed from partials) -> spoken/shown output,
  with indicator stages published at every step.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
from pathlib import Path
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .. import config, paths
from ..functions.base import Cancelled, FunctionError
from ..functions.registry import Registry
from ..models.backends import BackendError
from ..models.manager import ModelManager, ModelUnavailable
from ..util import desktop as desktop_name
from ..util import ends_like, graphical_env, normalize, same_onset, similarity, sound_similarity, speakable
from . import desktop as dk
from .audio import FRAME_MS, Playback, output_targets
from .context import FunctionContext
from .control import Control
from .history import History, new_entry
from .imports import Imports
from .intent import Decision, IntentProcessor
from .keyboard import Keyboard
from .listener import Listener, Session
from ..config import agent_memory
from .memory import Memory
from .online import Online
from .puppetry import Puppetry
from .summary import SummaryLog
from .timers import Timers
from .training import Training
from .triggers import Triggers
from .wikipedia import Wikipedia

log = logging.getLogger("jeeves.engine")


CHARACTER_VARIANTS = 3        # rewordings kept per plain line before they're reused without the model
CHARACTER_RETRY_EVERY = 8     # while the model is too slow for rewording: every Nth line tries it again


def speech_chunks(text: str, first: int = 40, later: int = 220, least: int = 140) -> list[str]:
    """A reply split for speaking: a short opening (whole sentences, at least ``first`` characters) and
    then groups of sentences of about ``later`` characters, so the opening can play while the rest is
    being synthesized. Short replies (under ``least`` characters, or one sentence) stay whole."""
    text = text.strip()
    if len(text) < least:
        return [text]
    sentences = [x for x in re.split(r"(?<=[.!?…])[\"'”’)]*\s+(?=[\"'“‘(]?[A-Z0-9])", text) if x.strip()]
    if len(sentences) < 2:
        return [text]
    parts: list[str] = []
    cur = ""
    for sent in sentences:
        limit = first if not parts else later
        if cur and len(cur) >= limit:
            parts.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        if parts and len(cur) < 25:
            parts[-1] = f"{parts[-1]} {cur}"         # no two-word tail synthesized on its own
        else:
            parts.append(cur)
    return parts if len(parts) > 1 else [text]


def resample(pcm: bytes, rate: int, to: int) -> bytes:
    """16-bit mono PCM at another rate (nearest sample: only for a voice that changed mid-reply)."""
    import array as _array
    a = _array.array("h", pcm[: len(pcm) // 2 * 2])
    if not a or rate == to or rate <= 0:
        return pcm
    n = int(len(a) * to / rate)
    return _array.array("h", (a[min(len(a) - 1, int(k * rate / to))] for k in range(n))).tobytes()


def muted(agent: dict[str, Any] | None, kind: str) -> bool:
    """Agent > Voice > Mute: "audio" = it never speaks; "visual" = nothing of it on screen. (A
    rudimentary agent's older Mute switch counts as audio.)"""
    a = agent or {}
    m = a.get("mute") or {}
    if kind == "audio" and (a.get("rudimentary") or {}).get("enabled") and (a.get("rudimentary") or {}).get("mute"):
        return True
    return bool(m.get(kind)) if isinstance(m, dict) else False



# What Whisper "hears" in silence or noise (it was trained on subtitles). Alone, these are dropped
# instead of being treated as a request -- otherwise a wake word followed by a pause got an answer.
NOISE_TEXTS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching", "thanks", "you", "bye",
    "bye bye", "so", "uh", "um", "hmm", "mm", "oh", "the end", "subtitles by the amaraorg community",
    "please subscribe", "like and subscribe", "blank audio", "silence", "music", "applause", "laughter",
    "inaudible", "foreign", "see you next time", "i'm sorry", "okay",
}


FACT_WORDS = (r"boss|build|item|weapon|armou?r|quest|mission|level|map|patch|update|release|version|mod|dlc|"
              r"achievement|trophy|walkthrough|guide|recipe|stats?|spawn|drop|location|where to find|how to get|"
              r"best|price|cost|lore|ending|character|skill|perk|class|season|episode|score|record|born|died|"
              r"population|capital|founded|invented")


ASK_LEAD = re.compile(
    r"^\s*(?:(?:please|hey|ok(?:ay)?|so|and|now)[\s,]+)*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:tell\s+me|show\s+me|teach\s+me|remind\s+me|explain(?:\s+to\s+me)?|let\s+me\s+know|"
    r"i\s+(?:want|need|would\s+like|wanna)\s+to\s+know|i'd\s+like\s+to\s+know|do\s+you\s+know|"
    r"i'?m\s+wondering|i\s+was\s+wondering|give\s+me\s+(?:the\s+)?(?:steps|instructions)\s+(?:for|on))"
    r"(?:\s+again)?[\s,:]+(?:exactly\s+|just\s+)?", re.I)


def looks_factual(text: str, agent: dict[str, Any] | None = None) -> bool:
    """A question that needs facts (better looked up than guessed): a wh-/how question naming
    something specific (a capitalised name, a number) or using game/product/fact words."""
    t = text.strip()
    # "tell me how to do the Parkour Reborn Wumpy", "can you explain what a wumpy is": the question inside
    lead = ASK_LEAD.match(t)
    if lead and re.match(r"(who|what|when|where|which|why|how)\b", t[lead.end():], re.I):
        t = t[lead.end():]
    low = t.lower()
    if not re.match(r"^(who|what|when|where|which|why|how|is|are|does|do|did|can|should|was|were|tell me about|"
                    r"explain)\b", low):
        return False
    if re.search(r"\b(you|your|yourself|we)\b|\b(what time|what day|today|tonight|what'?s up|how are|how'?s it going|"
                 r"should i|do you think|joke|story)\b", low) and not re.match(r"^how (do|can|would) you (do|get|make|"
                                                                               r"beat|unlock|find)\b", low):
        return False                              # about the conversation, or asking for an opinion
    if re.match(r"^(how (do|can|would|does|did) (i|one|people|someone|they)|how to)\s+\w+", low):
        return True                               # "how do I do a wumpy in parkour reborn": look it up
    if re.match(r"^(what|who)('s|\s+is|\s+are|\s+was|\s+were)\s+(a|an|the)?\s*[\w'-]+", low) and \
            re.search(r"\b(in|on|from|for)\s+[\w'.:-]+(\s+[\w'.:-]+){0,3}\s*\??$", low):
        return True                               # "what is a wumpy in parkour reborn"
    words = t.split()
    own = {"i", "i'm", "i've", "i'd", "i'll"}
    if agent:                                  # "how are you, Jeeves" isn't a fact to look up
        own |= {normalize(n) for n in list(agent.get("call_names", [])) + [agent.get("name", "")] if n}
    names = [w for w in words[1:] if w[:1].isupper() and normalize(w) not in own]
    return bool(names or re.search(r"\d", t) or re.search(rf"\b({FACT_WORDS})\b", low))


NAME_TAIL_FRAMES = 8          # 240 ms
TAIL_LIMIT = 0.6              # s: speech still going on when a listen opens is the name's own end, unless it
                              # lasts this long into the listen ("Jeeves open the mixer" in one breath)
REPLY_WAIT = 0.4              # s of quiet after the name before the wake-up reply ("Yes?")
HOLD_INDICATOR = "hold-to-listen"   # the indicator shown while the hold-to-listen keys are held
HOLD_SHOW_AFTER = 0.3         # s held before it shows (the keys may be part of a quick shortcut)


def is_noise_text(text: str) -> bool:
    t = re.sub(r"[\[\]()*♪.,!?'\"_-]", " ", text.lower())
    t = " ".join(t.split()).replace("amara org", "amaraorg")
    return not t or t in NOISE_TEXTS

class Waiter:
    """Something a running function is waiting on from the user."""

    def __init__(self, kind: str, ctx: FunctionContext, text: str, choices: list[str] | None = None) -> None:
        self.kind = kind                 # answer | confirm | click
        self.ctx = ctx
        self.text = text
        self.choices = choices
        self.event = threading.Event()
        self.value: Any = None

    def resolve(self, value: Any) -> None:
        self.value = value
        self.event.set()


class Engine:
    def __init__(self, publish: Callable[[str, Any], None] | None = None, start_io: bool = True) -> None:
        paths.ensure_all()
        self._publish_fn = publish or (lambda topic, data: None)
        self.settings = config.Settings()
        self.pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="jeeves-work")
        self.registry = Registry(self.settings)
        self.history = History(self.settings)
        self.memory = Memory(self.settings)
        self.summary = SummaryLog(self.settings)
        self.models = ModelManager(self.settings, self.publish, on_available=self._model_back)
        self.intent = IntentProcessor(self.settings, self.registry, self.models, self.history)
        self.intent.video_playing = lambda: bool(getattr(self, "video_state", {}).get("playing"))
        self.training = Training(self.settings, self.registry)
        self.intent.extra_examples = self.training.intent_examples
        self.control = Control(self.settings)
        self.on_exit: Callable[[], None] | None = None     # set by the server: stop the daemon
        self.interrupted: dict[str, list[str]] = {}       # agent -> requests paused by calling its name
        self.watchers: dict[str, Any] = {}                # agent -> Watcher (watching the screen)
        self.video_state: dict[str, Any] = {"playing": False}   # the Jeeves video player
        self.models.busy = lambda: bool(self.active) or bool(self.speaking)  # Hybrid Models ignores our own load
        self.puppetry = Puppetry(self.settings)
        self.online = Online(self.settings)
        self.wikipedia = Wikipedia(self.settings, self.publish)
        self.timers = Timers(self._timer_fired, self.publish, self.settings)
        self.triggers = Triggers(self.settings, self._trigger_fired)
        self.imports = Imports(self.registry, self.publish)
        from .jumpin import JumpIn
        self.jump_in = JumpIn(self)
        self.keyboard: Keyboard | None = Keyboard(self.settings, self._keybind, self._on_hold) if start_io else None
        self._hold_problem = self._hold_pending = ""      # hold-to-listen can't work: said / seen once
        self._lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self.speaking = 0
        self.speaking_until = 0.0
        self._reply_cache: dict[tuple, tuple[bytes, int]] = {}      # wake-up replies, synthesized once
        self._why_modes: dict[str, str] = {}
        self._wake_words: dict[str, list[Any]] = {}
        from .wakeclips import WakeClips
        self.clips = WakeClips(self.settings, self.training)
        self._mic_name, self._mic_checked = "", 0.0
        self._wakes: dict[str, list[float]] = {}                       # source -> when the wake model called (last minute)
        self._backup_guess: dict[str, str | None] = {}              # source -> the agent a missed call sounded like
        self._just_said: list[tuple[str, float, float]] = []          # (text, started, seconds) just spoken
        self._reply_tried: set[tuple] = set()
        self.active: dict[str, FunctionContext] = {}       # request id -> ctx
        self.indicators: dict[str, dict[str, Any]] = {}     # request id -> indicator state
        self.sessions: dict[str, Session] = {}              # audio source -> open session
        self.waiters: dict[str, Waiter] = {}                # request id -> waiter
        self.extended: dict[str, dict[str, Any]] = {}       # agent id -> {"parts": [...], ...}
        self.queue: list[tuple[str, Any]] = []              # requests waiting for a suspended model
        self.listeners: dict[str, Listener] = {}
        self._app_cache: tuple[float, list[str], str] = (0.0, [], "")
        self._overlays: dict[str, subprocess.Popen] = {}
        self._overlay_fails: dict[str, tuple[int, float]] = {}
        self._stop = threading.Event()
        self.start_io = start_io

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.start_io:
            return
        self.models.start()
        self.models.warm_up = self._warm_intent
        self.models.preload()
        self.triggers.start()
        if self.keyboard:
            self.keyboard.start()
        self.apply_settings()
        threading.Thread(target=self._housekeeping, daemon=True, name="jeeves-housekeeping").start()

    def stop(self) -> None:
        self._stop.set()
        self.abort()                                # (stops whatever it's saying)
        for lst in self.listeners.values():
            lst.stop()
        self.models.stop()
        self.timers.stop()
        self.triggers.stop()
        if self.keyboard:
            self.keyboard.stop()
        self.control.close()
        for proc in self._overlays.values():
            proc.terminate()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ on/off switch
    def is_on(self) -> bool:
        return bool(self.settings.get("general.enabled", True))

    def set_enabled(self, on: bool) -> bool:
        """The master switch (GUI, `jeeves --toggle`, toggle keybind)."""
        self.settings.set("general.enabled", bool(on))
        self._apply_power()
        self.apply_settings()
        self.publish("settings", {"changed": ["general.enabled"]})
        return self.is_on()

    def toggle(self) -> bool:
        return self.set_enabled(not self.is_on())

    def _apply_power(self) -> None:
        """Off: stop every agent, stop listening, unload every model (frees the memory).
        On: reload the chosen models in the background."""
        on = self.is_on()
        if on == getattr(self, "_was_on", None):
            return
        first = not hasattr(self, "_was_on")
        self._was_on = on
        if on:
            if not first:
                self.models.preload()
                self.publish("notice", {"text": "Jeeves is on"})
        else:
            self.abort()
            self.models.unload_all()
            self.publish("notice", {"text": "Jeeves is off"})
            if self.on_exit is not None:
                # off = the daemon stops; turning on starts a fresh one (GUI, `jeeves on`, `jeeves --toggle`).
                # A moment's delay lets the reply to the request that switched it off go out first.
                threading.Timer(0.5, self.on_exit).start()

    def _refuse_if_off(self) -> bool:
        if self.is_on():
            return False
        self.publish("notice", {"text": "Jeeves is off (turn it on with jeeves --toggle)"})
        return True

    def apply_settings(self) -> None:
        """Start/stop listeners to match settings (called after any change)."""
        if not self.start_io:
            return
        with self._apply_lock:
            self._apply_settings_locked()
        self.run_async(self._prewarm_wake_replies)          # new or changed wake-up replies, voices

    def _apply_settings_locked(self) -> None:
        self._apply_power()
        from ..functions.partials import youtube as yt
        yt.configure(str(self.settings.get("youtube.cookies_from_browser", "auto") or "auto"),
                     str(self.settings.get("youtube.cookies_file", "") or ""))
        if self.is_on():
            # "Jeeves-Microphone" always exists while Jeeves is on, so Discord/OBS can list it before an
            # agent first speaks into it (it carries your own mic in the meantime). Nothing here may stop
            # the listeners below from starting.
            try:
                from .audio import ensure_virtual_mic
                ensure_virtual_mic(self.settings.get("audio.virtual_mic_sink", "jeeves-mic"),
                                   bool(self.settings.get("audio.virtual_mic_include_mic", True)),
                                   self.settings.get("audio.microphone", ""))
            except Exception:
                log.exception("couldn't set up Jeeves-Microphone")
        try:
            self.models.prune()
        except Exception:
            log.exception("model cleanup failed")
        wanted = self._wanted_sources()
        for src in list(self.listeners):
            if src not in wanted:
                self.listeners.pop(src).stop()
        for src in wanted:
            if src not in self.listeners or not self.listeners[src].is_alive():
                lst = Listener(self, src)
                self.listeners[src] = lst
                lst.start()
        self.publish("status", self.status())

    def _wanted_sources(self) -> set[str]:
        wanted = set()
        if self._needs_source("microphone"):
            wanted.add("microphone")
        if self._needs_source("desktop"):
            wanted.add("desktop")
        if self.is_on():
            for a in self.active_agents().values():          # agents listening to one exact device
                if a.get("listen_to") == "device" and a.get("listen_device"):
                    if self._is_the_mic(a["listen_device"]):
                        # that device IS the microphone: the microphone's listener hears for it. A second
                        # listener on the same mic woke every name twice (and ran Whisper twice)
                        if self._needs_source("microphone") or self.call_names("microphone"):
                            wanted.add("microphone")
                        continue
                    src = f"device:{a['listen_device']}"
                    said = self.__dict__.setdefault("_said_devices", set())
                    if src not in said and self._mic_name:
                        said.add(src)
                        log.info("%s listens to %s on its own; the microphone listener records %s", a.get("name"),
                                 a["listen_device"], self._mic_name)
                    if self.call_names(src) or self.jump_in.wants(src):
                        wanted.add(src)
        return wanted

    def _listeners_stale(self) -> bool:
        """The running listeners don't match what's needed now. Settings changes already restart them;
        this catches the rest: an agent that only listens while some app is open or focused (no
        settings change when you open the app, so its microphone never started), and a listener
        thread that died."""
        alive = {src for src, lst in self.listeners.items() if lst.is_alive()}
        return alive != self._wanted_sources()

    def _needs_source(self, source: str) -> bool:
        if not self.is_on():
            return False
        if source == "microphone":
            # only while something needs it: always-on wake words, the summary log, keyword
            # triggers, or a voice request / question that is listening right now
            if self.sessions.get("microphone") is not None or self.answer_pending("microphone"):
                return True
            if self.summary.enabled() and "microphone" in (self.settings.get("summary.sources") or []):
                return True
            if any(t.get("event") == "audio_keyword" and t.get("source", "microphone") in ("microphone", "both")
                   for t in self.triggers.all()):
                return True
            if self.jump_in.wants("microphone"):
                return True
            # (hold-to-listen keeps it open too: the start of a name said as you press the keys is in it)
            return bool(self.listens_for_names() and self.call_names("microphone"))
        if self.summary.enabled() and "desktop" in (self.settings.get("summary.sources") or []):
            return True
        if any(t.get("event") == "audio_keyword" and t.get("source") in ("desktop", "both")
               for t in self.triggers.all()):
            return True
        if self.jump_in.wants("desktop"):
            return True
        return any(a.get("enabled", True) and a.get("listen_to") in ("desktop", "both")
                   for a in self.agents().values())

    def _housekeeping(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.imports.scan_dropbox()
                self._ensure_overlay()
                if self.start_io and self._listeners_stale():
                    self.apply_settings()
                self._check_hold_to_listen()
            except Exception:
                log.exception("housekeeping failed")

    # ------------------------------------------------------------------ events
    def publish(self, topic: str, data: Any) -> None:
        try:
            self._publish_fn(topic, data)
        except RuntimeError as exc:
            if "closed" not in str(exc):            # (shutting down: nobody left to tell)
                log.exception("publish failed")
        except Exception:
            log.exception("publish failed")

    def run_async(self, fn: Callable[..., Any], *args: Any) -> None:
        self.pool.submit(self._guard, fn, *args)

    @staticmethod
    def _guard(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background task failed")

    def set_indicator(self, request_id: str, agent_id: str | None, stage: str, detail: str = "", **extra: Any) -> None:
        agent = self.agents().get(agent_id or "", {})
        state = {"request": request_id, "agent": agent_id, "agent_name": agent.get("name", ""),
                 "color": agent.get("indicator_color"), "stage": stage, "detail": detail, "time": time.time()}
        state.update(extra)
        ctx = self.active.get(request_id)
        session = next((s for s in self.sessions.values() if s.request_id == request_id), None)
        state["suspended"] = bool((ctx is not None and ctx.suspend_event.is_set()) or
                                  (session is not None and session.suspended))
        if muted(agent, "visual") and stage not in ("asking", "unclear"):
            state["hidden"] = True                   # visual mute: nothing on screen (unless it needs you)
        with self._lock:
            if stage == "idle":
                self.indicators.pop(request_id, None)
            else:
                self.indicators[request_id] = state
        self.publish("indicator", state)

    def flash_unavailable(self, agent_id: str | None, reason: str) -> None:
        rid = f"unavail-{time.time():.3f}"
        self.set_indicator(rid, agent_id, "unavailable", reason)
        threading.Timer(1.6, lambda: self.set_indicator(rid, agent_id, "idle")).start()

    # ------------------------------------------------------------------ agents
    def agents(self) -> dict[str, dict[str, Any]]:
        return self.settings.get("agents", {}) or {}

    def _apps(self) -> tuple[list[str], str]:
        now = time.time()
        if now - self._app_cache[0] > 2.0:
            try:
                f = dk.focused()
                focused = f.app if f else ""
            except dk.DesktopUnavailable:
                focused = ""
            self._app_cache = (now, dk.open_apps() + dk.processes(), focused)
        return self._app_cache[1], self._app_cache[2]

    def agent_active(self, agent: dict[str, Any]) -> bool:
        if not agent.get("enabled", True):
            return False
        rules = [agent.get(k) for k in ("enable_when_open", "enable_when_focused", "disable_when_open",
                                         "disable_when_focused")]
        if not any(rules):
            return True
        open_apps, focused = self._apps()
        if agent.get("disable_when_open") and dk.app_matches(agent["disable_when_open"], open_apps):
            return False
        if agent.get("disable_when_focused") and dk.app_matches(agent["disable_when_focused"], [focused]):
            return False
        need_open, need_focus = agent.get("enable_when_open"), agent.get("enable_when_focused")
        if need_open or need_focus:
            return bool((need_open and dk.app_matches(need_open, open_apps)) or
                        (need_focus and dk.app_matches(need_focus, [focused])))
        return True

    def active_agents(self) -> dict[str, dict[str, Any]]:
        return {aid: a for aid, a in self.agents().items() if self.agent_active(a)}

    def _is_the_mic(self, device: str) -> bool:
        """Is this device the one the microphone listener records (the default mic)? Asked of pactl at
        most every 10 s."""
        now = time.time()
        chosen = self.settings.get("audio.microphone", "") or ""
        if chosen:                                   # (the microphone picked in Settings is what it records)
            self._mic_name = chosen
        elif now - self._mic_checked > 10:
            from .audio import real_mic_source
            try:
                self._mic_name = real_mic_source() or ""
            except Exception:  # noqa: BLE001
                self._mic_name = ""
            self._mic_checked = now
        return bool(device) and device == self._mic_name

    def _listens(self, agent: dict[str, Any], source: str) -> bool:
        lt = agent.get("listen_to", "user")
        if lt == "device":
            dev = agent.get("listen_device") or ""
            if source == "microphone" and self._is_the_mic(dev):
                return True
            return bool(dev) and source == f"device:{dev}"
        return lt == "both" or (lt == "user" and source == "microphone") or (lt == "desktop" and source == "desktop")

    def call_names(self, source: str) -> list[str]:
        names = []
        for a in self.active_agents().values():
            if self._listens(a, source):
                names += [n.lower() for n in a.get("call_names", []) if n.strip()]
        return sorted(set(names))

    def wake_names(self, source: str) -> list[str]:
        """Every enabled agent's call names on this source, app rules or not (the wake grammar)."""
        names = []
        for a in self.agents().values():
            if a.get("enabled", True) and self._listens(a, source):
                names += [n.lower() for n in a.get("call_names", []) if n.strip()]
        return sorted(set(names))

    def recent_for(self, agent_id: str | None, agent: dict[str, Any], n: int, own_only: bool = False,
                   exclude: str | None = None, only_agent: str | None = None) -> list[dict[str, Any]]:
        """Recent requests an agent may know about: its own, typed ones, and ones spoken on a
        source it listens to -- never what you said into the mic for a mic-less agent."""
        if n <= 0:
            return []

        def can_hear(source: str) -> bool:
            return not source.startswith("voice:") or self._listens(agent, source.split(":", 1)[1])
        return self.history.recent(n, agent=agent_id if own_only else only_agent, exclude=exclude,
                                   viewer=agent_id, can_hear=can_hear)

    def agent_by_name(self, name: str, source: str | None = None) -> str | None:
        """The agent called this; with a source, only agents that listen to it."""
        n = normalize(name)
        if source is not None:
            hearing = {aid: a for aid, a in self.active_agents().items() if self._listens(a, source)}
            for aid, a in hearing.items():
                if n == normalize(a.get("name", "")) or any(n == normalize(c) for c in a.get("call_names", [])):
                    return aid
            best = max(((similarity(n, c), aid) for aid, a in hearing.items()
                        for c in a.get("call_names", []) + [a.get("name", "")]), default=(0.0, None))
            return best[1] if best[0] >= 0.75 else None
        for aid, a in self.agents().items():
            if n == normalize(aid) or n == normalize(a.get("name", "")) or \
                    any(n == normalize(c) for c in a.get("call_names", [])):
                return aid
        best, score = None, 0.0
        for aid, a in self.agents().items():
            for c in a.get("call_names", []) + [a.get("name", "")]:
                s = similarity(n, c)
                if s > score:
                    best, score = aid, s
        return best if score >= 0.75 else None

    def threshold(self, agent: dict[str, Any]) -> float:
        if agent.get("threshold") is not None:
            return float(agent["threshold"])
        if self.settings.get("wake_word.global_threshold_enabled", True):
            return float(self.settings.get("wake_word.global_threshold", 0.6))
        return 0.5

    def split_agent(self, text: str, source: str | None = None) -> tuple[str | None, str, float]:
        """Find a call name near the start of text -> (agent id, rest of text, score)."""
        named = self.named_agent(text, source, within=3)
        if named is not None:                   # (the longest name wins: "Evil Jeeves" over "Jeeves")
            return named, self.strip_name(text, named, within=3)[1], 1.0
        words = text.strip()
        best: tuple[str | None, str, float] = (None, text, 0.0)
        for aid, a in self.active_agents().items():
            if source and not self._listens(a, source):
                continue
            for cname in sorted(a.get("call_names", []), key=len, reverse=True):
                m = re.match(rf"^\W*(?:hey\s+|ok\s+|okay\s+)?({re.escape(cname)})\b[\s,.:!?-]*(.*)$", words, re.I | re.S)
                if m:
                    return aid, m.group(2).strip(), 1.0
                # fuzzy: compare the first words with the name
                n = len(cname.split())
                head = " ".join(re.findall(r"[\w']+", words)[:n + 1][-n - 1:][:n]) if words else ""
                score = similarity(head, cname)
                if score > best[2]:
                    rest = re.sub(rf"^\W*(?:\S+\W+){{{n}}}", "", words, count=1).strip()
                    best = (aid, rest, score)
        if best[0] is not None and best[2] >= self.threshold(self.agents()[best[0]]):
            return best
        return None, text, best[2]

    def _warm_intent(self) -> None:
        """Each agent's intent prompt, read by the intent model ahead of the first request."""
        for aid, agent in list(self.agents().items())[:4]:
            if agent.get("enabled", True):
                try:
                    self.intent.warm(dict(agent, id=aid))
                except Exception as exc:  # noqa: BLE001 -- a model that isn't there yet, an endpoint that's off
                    log.info("couldn't warm up the intent model for %s: %s", aid, exc)
                    return

    # ------------------------------------------------------------------ listening
    def stt_spells_names(self, agent: dict[str, Any] | None = None) -> bool:
        """Whether speech recognition writes call names out when they're said. Whisper is given them (its
        initial prompt), so "Jeeves" comes out as Jeeves -- and "geez" as geez, which is how a call is told
        from a sound-alike. Vosk's speech recognition can't be given them: "Jeeves" comes out as "the eaves",
        so looking for the name in its transcript turned down every call."""
        from ..models import catalog
        entry = catalog.get(self.models.model_id("stt", agent))
        return entry is None or entry.engine != "vosk"

    def hold_keys(self) -> list[str] | None:
        """Hold-to-listen: the keys that must be held for agent names to be listened for (None: it's off,
        names are always listened for)."""
        if not self.settings.get("wake_word.hold_to_listen", False):
            return None
        keys = self.settings.get("wake_word.hold_keys") or []
        return [str(k).strip().upper() for k in ([keys] if isinstance(keys, str) else keys) if str(k).strip()]

    def listens_for_names(self) -> bool:
        """Agent names are listened for: always, or while the hold-to-listen keys are held."""
        return bool(self.settings.get("wake_word.enabled", True) or self.hold_keys() is not None)

    def may_call(self, source: str, t0: float, t1: float) -> bool:
        """Could speech heard between t0 and t1 call an agent by name? Always -- unless hold-to-listen is on:
        then only if the keys were held at some moment of it."""
        keys = self.hold_keys()
        if keys is None:
            return True
        return bool(keys) and self.keyboard is not None and self.keyboard.held_during(keys, t0, t1)

    def _on_hold(self, held: bool) -> None:
        """The hold-to-listen keys were pressed or let go: show that agents are listening for their names
        while they're held (not straight away: the keys can be part of a quick shortcut)."""
        if held:
            def show() -> None:
                kb, keys = self.keyboard, self.hold_keys()
                if kb is not None and keys and kb.holding(keys) and self.is_on() and \
                        self.sessions.get("microphone") is None:
                    self.set_indicator(HOLD_INDICATOR, None, "listening", "say an agent's name",
                                       source="microphone", mode="hold")
            timer = threading.Timer(HOLD_SHOW_AFTER, show)
            timer.daemon = True
            timer.start()
        elif HOLD_INDICATOR in self.indicators:
            self.set_indicator(HOLD_INDICATOR, None, "idle")

    def _check_hold_to_listen(self) -> None:
        """Hold-to-listen on, but no way to see the keys: names would never be heard. Say so (once)."""
        keys = self.hold_keys()
        problem = ""
        if keys is not None and self.is_on() and self.start_io:
            kb = self.keyboard
            if not keys:
                problem = "Hold-to-listen is on but has no keys to hold, so no agent hears its name."
            elif kb is None or not kb.status.startswith("watching"):
                problem = (f"Hold-to-listen is on, but Jeeves can't read the keyboard "
                           f"({kb.status if kb else 'off'}), so no agent hears its name.")
        # (seen twice in a row: right after it's turned on, the keyboard takes a moment to be read)
        if problem and problem == self._hold_pending and problem != self._hold_problem:
            log.warning("%s", problem)
            self.publish("notice", {"text": problem + " (Listening & Keys)"})
            self._hold_problem = problem
        elif not problem:
            self._hold_problem = ""
        self._hold_pending = problem

    def detection_modes(self, source: str) -> set[str]:
        modes: set[str] = set()
        summary = self.summary.enabled() and source in (self.settings.get("summary.sources") or [])
        keyword_triggers = any(t.get("event") == "audio_keyword" and t.get("source", "microphone") in (source, "both")
                               for t in self.triggers.all())
        names = self.call_names(source)
        # (Summary used to switch the wake word model off -- names were then only found once a whole
        # sentence was transcribed, seconds late and with no mic indicator while you talked. Both run now:
        # the wake model hears calls, transcription feeds the Summary and catches names it missed.)
        wake_on = self.listens_for_names()
        self._why_modes[source] = self._modes_reason(source, names, wake_on)
        if names and wake_on:
            engine = self.settings.get("wake_word.engine", "vosk")
            if engine == "vosk" and self.models.wake_spotter() is not None:
                modes.add("vosk")
                if self.settings.get("wake_word.stt_backup", True) and "stt" not in self.models.suspended \
                        and self.stt_spells_names():
                    modes.add("backup")              # (it looks for the name in what speech recognition writes)
            else:
                modes.add("transcribe")
        if summary or keyword_triggers or (names and self.summary.enabled()) or self.jump_in.wants(source):
            modes.add("transcribe")
        return modes

    def _modes_reason(self, source: str, names: list[str], wake_on: bool) -> str:
        """Why names are (or aren't) heard by the wake word model on this source -- for the log, since
        without it a call is only noticed once speech recognition has the whole sentence (seconds later,
        and with no mic indicator while you talk)."""
        if not names:
            return "no agent listens here"
        if not self.listens_for_names():
            return "names aren't listened for"
        engine = self.settings.get("wake_word.engine", "vosk")
        if engine != "vosk":
            return f"wake_word.engine is {engine!r}: names are found in transcribed text (no wake word model)"
        if self.models.wake_spotter() is None:
            return ("the wake word model (wake_word.model) isn't chosen, installed or loadable: names are found "
                    "in transcribed text instead")
        if self.summary.enabled():
            return "the wake word model hears names; everything is transcribed for the Summary too"
        return "the wake word model hears names"

    def open_session(self, source: str, agent_id: str | None, mode: str) -> Session:
        s = Session(source=source, agent_id=agent_id, mode=mode,
                    max_seconds=float(self.settings.get("general.max_request_seconds", 60)))
        if mode == "extended":
            s.max_seconds = 3600
        with self._lock:
            old = self.sessions.get(source)
            if old is not None:
                old.ended = True
            self.sessions[source] = s
        if old is not None:                      # its indicator goes with it
            self.set_indicator(old.request_id, old.agent_id, "idle")
        if HOLD_INDICATOR in self.indicators:    # heard a name while the hold-to-listen keys were held
            self.set_indicator(HOLD_INDICATOR, None, "idle")
        stage = "listening"
        self.set_indicator(s.request_id, agent_id, stage, "extended prompt mode" if mode == "extended" else "",
                           source=source, mode=mode)
        return s

    def end_session(self, s: Session) -> None:
        from .audio import FRAME_MS
        with self._lock:
            if self.sessions.get(s.source) is s:
                del self.sessions[s.source]
        s.ended = True
        self.run_async(self.apply_settings)      # the mic may not be needed any more
        took = time.time() - s.started
        speech = s.voiced_frames * FRAME_MS / 1000
        if s.mode == "request" and s.preroll is not None and not getattr(s, "clip_id", None):
            # kept to listen back to (Training > Recent wake-ups): what woke it, and what followed
            try:
                s.clip_id = self.clips.add(b"".join(s.preroll) + s.pcm(), s.agent_id, s.source,
                                           getattr(s, "wake_name", ""), s.wake_conf, getattr(s, "wake_words", []))
                if not s.got_speech:
                    self.clips.update(s.clip_id, outcome="nothing said after the name")
            except OSError:
                log.exception("couldn't keep the wake-up clip")
        log.info("listen ended (%s, %s): %.1fs long, %.2fs of speech%s", s.agent_id, s.mode, took, speech,
                 " -- hit the time limit" if took >= s.max_seconds - 0.5 else "")
        # only a click or a breath: nothing to transcribe. (This used to need 0.3 s of speech, which a short
        # "stop" or anything said over loud music often didn't reach -- those got "Didn't catch that".
        # Speech recognition's own silence filter and the name check below catch the rest.)
        if s.got_speech and (s.mode == "request" and speech < 0.12 or s.mode == "answer" and speech < 0.1):
            log.info("  too little speech to be a request; ignored")
            self.clips.update(getattr(s, "clip_id", None), outcome="too little speech after the name")
            s.got_speech = False
            if s.interrupting:
                self._resume(s.interrupting)
            elif s.preroll is None or s.trusted():
                self._didnt_catch(s)             # you opened this listen (a key, the mic, a clear call): say so
            else:
                self.set_indicator(s.request_id, s.agent_id, "idle")   # most likely not a call at all
            return
        if not s.got_speech:
            self.set_indicator(s.request_id, s.agent_id, "idle")
            if s.interrupting:                   # called by name, then nothing said: carry on
                self._resume(s.interrupting)
            return
        self.run_async(self._finish_session, s)

    def _finish_session(self, s: Session) -> None:
        pcm = s.pcm()
        spells = self.stt_spells_names(self.agents().get(s.agent_id or ""))
        if s.preroll and s.mode == "request" and spells:
            pcm = b"".join(s.preroll) + pcm          # the name too: speech recognition confirms it was said
        if s.mode == "training":
            item = self.training.save_recording(s.text, pcm)
            self.set_indicator(s.request_id, s.agent_id, "idle")
            self.publish("training_recording", item)
            return
        agent = self.agents().get(s.agent_id or "", {})
        try:
            text = self.transcribe(pcm, agent)
        except ModelUnavailable as exc:
            self.flash_unavailable(s.agent_id, exc.reason)
            self.set_indicator(s.request_id, s.agent_id, "idle")
            if exc.queueable and s.mode == "request":
                self.queue.append(("voice", {"agent": s.agent_id, "pcm": pcm}))
            return
        except Exception:                       # never leave a request stuck on "listening" with no answer
            log.exception("transcribing the request failed")
            self.flash_unavailable(s.agent_id, "speech recognition failed (see the log)")
            self.set_indicator(s.request_id, s.agent_id, "idle")
            return
        clip = getattr(s, "clip_id", None)
        self.clips.update(clip, heard=text, outcome="heard")
        if s.mode == "request" and s.preroll is not None and spells:
            other = self.named_agent(text, s.source)
            if other is not None and other != s.agent_id:
                # the wake model mixed up two names ("Jeeves" / "Evil Jeeves"); speech recognition heard the
                # whole thing: the call goes to the agent it names
                log.info("  woke %s, but speech recognition heard %r: it's for %s", s.agent_id, text, other)
                self.set_indicator(s.request_id, s.agent_id, "idle")
                s.agent_id = other
                self.set_indicator(s.request_id, other, "transcript", text)
            found, rest = self.strip_name(text, s.agent_id)
            if found:
                text = rest
            elif not s.trusted():
                # the wake model hears "geez", "believes", "cheese" as "Jeeves" now and then; speech
                # recognition, which heard the whole phrase, says the name wasn't said: not for us
                log.info("  woke on the name, but speech recognition heard '%s': not the name, ignored", text)
                self.clips.update(clip, outcome="ignored: speech recognition didn't hear the name")
                self.set_indicator(s.request_id, s.agent_id, "misheard")       # spins away red
                rid, aid0 = s.request_id, s.agent_id
                threading.Timer(0.9, lambda: self.set_indicator(rid, aid0, "idle")).start()
                if s.interrupting:
                    self._resume(s.interrupting)
                return
        self.set_indicator(s.request_id, s.agent_id, "transcript", text)
        if s.mode == "answer":
            self.set_indicator(s.request_id, s.agent_id, "idle")
            self._deliver_answer(s.agent_id, text)
            return
        if s.mode == "request" and is_noise_text(text):
            log.info("ignored '%s' (what speech recognition hears in silence)", text)
            text = ""
        if s.mode == "request" and not s.interrupting and self._no_request_in(text):
            # only the name (or its tail, a breath, "um", another agent's name): the request hasn't been
            # said yet -- answering this is the "instant reply about nothing". Listen once more for it.
            if not s.retried:
                log.info("  heard only '%s': still listening for the request", text)
                self.set_indicator(s.request_id, s.agent_id, "idle")      # (the new listen has its own)
                again = self.open_session(s.source, s.agent_id, "request")
                again.retried, again.manual = True, s.manual
                self.apply_settings()
                return
            log.info("  heard only '%s' again; giving up", text)
            text = ""
        if s.interrupting:
            if self._after_interruption(s.interrupting, text):
                self.set_indicator(s.request_id, s.agent_id, "idle")
                return
        if not text.strip():
            log.info("  nothing understandable in it")
            self.clips.update(clip, outcome="nothing understandable after the name")
            self._didnt_catch(s)
            return
        log.info("  heard: %s", text)
        self.clips.update(clip, outcome=f"ran as a request for {s.agent_id}")
        self.set_indicator(s.request_id, s.agent_id, "thinking")     # on from the intent step, not at "say"
        res = self.handle_text(text, s.agent_id, source=f"voice:{s.source}", request_id=s.request_id,
                               manual=s.manual)
        if not (isinstance(res, dict) and res.get("id") == s.request_id and not res.get("duplicate")):
            self.set_indicator(s.request_id, s.agent_id, "idle")   # not run as a request (no agent named, an answer ...)

    def strip_name(self, text: str, agent_id: str | None, within: int = 10) -> tuple[bool, str]:
        """(was the agent's name said, the rest after it): "Okay so, Jeeves, set a timer" -> (True,
        "set a timer"). Looks at the first `within` words only and accepts spellings that sound the same
        ("Jeevs", "Geeves") -- not merely similar ones ("Reeves", "geez")."""
        at = self._name_at(text, agent_id, within)
        return (True, at[2]) if at is not None else (False, text)

    def _name_at(self, text: str, agent_id: str | None, within: int = 10) -> tuple[int, int, str] | None:
        """Where the agent's name is said: (word index, how many words, the rest after it), or None."""
        agent = self.agents().get(agent_id or "", {})
        names = sorted((normalize(n).split() for n in list(agent.get("call_names", [])) + [agent.get("name", "")]
                        if n), key=len, reverse=True)
        words = list(re.finditer(r"[\w'-]+", text))
        for k in range(min(within, len(words))):
            for parts in names:
                if not parts or k + len(parts) > len(words):
                    continue
                said = [normalize(words[k + j].group()) for j in range(len(parts))]
                if all(a == b or same_onset(a, b) and (similarity(a, b) >= 0.8 or sound_similarity(a, b) >= 0.9)
                       for a, b in zip(said, parts)):
                    rest = text[words[k + len(parts) - 1].end():]      # as written: paths, quotes and all
                    return k, len(parts), re.sub(r"^[\s,.:;!?-]+", "", rest).strip()
        return None

    def named_agent(self, text: str, source: str | None = None, within: int = 10) -> str | None:
        """Which agent the words name: the earliest name, and of names starting at the same word the
        longest -- "Evil Jeeves, ..." is Evil Jeeves, not Jeeves (whose name is in it too)."""
        best: tuple[int, int, str] | None = None
        for aid, a in self.active_agents().items():
            if source and not self._listens(a, source):
                continue
            at = self._name_at(text, aid, within)
            if at is None:
                continue
            # a shorter name ending where a longer one ends is the longer one's tail: "evil JEEVES"
            key = (at[0] + at[1], -at[1])
            if best is None or key < (best[0], best[1]):
                best = (key[0], key[1], aid)
        return best[2] if best else None

    FILLER = set("um uh uhm erm er hm hmm mm hey hi oh ok okay so yeah yes no the a and you i like well right "
                 "please thanks thank".split())

    def _no_request_in(self, text: str) -> bool:
        """Nothing in this transcript but call names and filler."""
        names = {w for a in self.agents().values() for n in [a.get("name", "")] + list(a.get("call_names", []))
                 for w in normalize(n).split()}
        words = normalize(text).split()
        return all(w in self.FILLER or any(similarity(w, n) >= 0.75 for n in names) for w in words)

    def transcribe(self, pcm: bytes, agent: dict[str, Any] | None = None) -> str:
        """Speech to text. A failed or timed-out speech server is restarted and tried once more;
        if that fails too it's reported as ModelUnavailable, which every caller handles -- before,
        it escaped as an unexpected error and the request vanished with no answer."""
        lang = self.settings.get("models.stt.language", "en")
        now = time.time()
        if now < getattr(self, "_stt_rest_until", 0.0):
            raise ModelUnavailable("stt", "speech recognition keeps crashing; resting it for a few minutes")
        for attempt in (1, 2):
            stt = self.models.stt(agent)
            try:
                return stt.transcribe(pcm, prompt=self.stt_prompt(), language=lang)
            except (BackendError, OSError) as exc:
                if getattr(stt, "retired", False) and attempt == 1:
                    # the speech model was switched (or unloaded) while it worked: not a crash -- the new one
                    log.info("speech model changed mid-transcription; trying again with the new one")
                    continue
                # crashing again and again (each restart reloading the model): rest it for 5 minutes
                fails = [t for t in getattr(self, "_stt_fails", []) if now - t < 300] + [now]
                self._stt_fails = fails
                if len(fails) >= 4:
                    self._stt_rest_until = now + 300
                    self._stt_fails = []
                    log.warning("speech recognition crashed %d times in 5 minutes; resting it for 5", len(fails))
                    self.publish("notice", {"text": "Speech recognition keeps crashing, so it's resting for 5 "
                                                    "minutes (see `journalctl --user -u jeeves`). A smaller "
                                                    "speech model (Models) may help."})
                    try:
                        stt.unload()
                    except Exception:  # noqa: BLE001
                        pass
                    raise ModelUnavailable("stt", f"speech recognition failed: {exc}") from exc
                if attempt == 2:
                    raise ModelUnavailable("stt", f"speech recognition failed: {exc}") from exc
                log.warning("speech recognition failed (%s); restarting it and trying again", exc)
                try:
                    stt.unload()
                except Exception:  # noqa: BLE001
                    pass
        return ""

    def stt_prompt(self) -> str:
        """Whisper's initial prompt: the agents' names (so "Jeeves" is spelled right, which the wake
        check relies on) and the trained vocabulary."""
        names = sorted({n.strip() for a in self.agents().values() if a.get("enabled", True)
                        for n in [a.get("name", "")] + list(a.get("call_names", [])) if n.strip()},
                       key=str.lower)
        vocab = self.training.initial_prompt()
        lead = (", ".join(names) + ".") if names else ""
        return " ".join(p for p in (lead, vocab) if p)

    # ------------------------------------------------------------------ "did you say Jeeves?"
    MAYBE_SECONDS = 2.5

    def voice_level(self, request_id: str, level: float, floor: float, voiced: bool) -> None:
        """While it listens: how loud you are, 0..1 above the room's background (the on-screen mic moves
        with your voice). Only sent while it changes, so a quiet room sends nothing."""
        import math
        db = 20 * math.log10(max(1.0, level))
        base = 20 * math.log10(max(1.0, floor)) + 4
        v = max(0.0, min(1.0, (db - base) / 26.0)) if voiced or db > base + 6 else 0.0
        last = self.__dict__.setdefault("_levels", {})
        if abs(last.get(request_id, -1.0) - v) < 0.04 and v < 0.02:
            return
        last[request_id] = v
        if len(last) > 32:
            last.clear()
        self.publish("level", {"request": request_id, "level": round(v, 3)})

    def wake_maybe(self, source: str, name: str) -> None:
        """The wake model thinks it's hearing a name (the moment it appears, before it's sure): the agent's
        indicator shows at once, so you know to keep talking. If it turns out not to be a call, it spins
        away in red (misheard)."""
        if not self.is_on() or self.previewing() or self.sessions.get(source) is not None:
            return
        aid = self.agent_by_name(name, source)
        if aid is None:
            return
        rid = f"maybe-{source}"
        maybes = self.__dict__.setdefault("_maybes", {})
        maybes[source] = (rid, aid, time.time())
        self.set_indicator(rid, aid, "listening", "")

        def expire() -> None:
            cur = maybes.get(source)
            if cur is not None and cur[0] == rid and time.time() - cur[2] >= self.MAYBE_SECONDS - 0.05:
                self.clear_maybe(source, misheard=True)
        threading.Timer(self.MAYBE_SECONDS, expire).start()

    def clear_maybe(self, source: str, misheard: bool = False, hold: bool = False) -> None:
        """hold: still checking (speech recognition): keep it up a little longer."""
        maybes = self.__dict__.setdefault("_maybes", {})
        cur = maybes.get(source)
        if cur is None:
            return
        rid, aid, _t = cur
        if hold:
            maybes[source] = (rid, aid, time.time())
            return
        maybes.pop(source, None)
        if misheard:
            self.set_indicator(rid, aid, "misheard", "")
            threading.Timer(0.9, lambda: self.set_indicator(rid, aid, "idle")).start()
        else:
            self.set_indicator(rid, aid, "idle")

    NEAR_MISS = 0.75

    def on_wake(self, source: str, name: str, conf: float, after: list[bytes], threshold: float | None = None,
                words: int = 0, name_audio: list[bytes] | None = None,
                after_voiced: list[bool] | None = None, wake_words: list[Any] | None = None) -> None:
        """The wake word model heard a call name. after: the audio since the name (after_voiced: the
        speech detector's verdict on each frame), words: how many words it heard after the name,
        name_audio: the name itself (speech recognition checks it was really said)."""
        self._wake_words[source] = list(wake_words or [])
        if not self.is_on() or self.previewing():
            return
        now = time.time()
        self._wakes[source] = [t for t in self._wakes.get(source, []) if now - t < 60] + [now]
        cur = self.sessions.get(source)
        if cur is not None:
            if cur.mode != "request" or time.time() - cur.started < 8:
                return
            log.info("wake word during a %.0fs-old listen: starting over", time.time() - cur.started)
            cur.ended = True                     # a listen that long is stuck on noise: start over
        aid = self.agent_by_name(name, source)
        if aid is None:
            return
        agent = self.agents()[aid]
        thr = self.threshold(agent)
        if self.answer_pending(source) == aid and conf >= thr * 0.5:
            # it asked you something and you answer with its name first ("Jeeves, yes"): that's the answer
            log.info("wake '%s' while %s waits for an answer: listening for the answer", name, aid)
            s = self.open_session(source, aid, "answer")
            s.frames = list(after)
            return
        if conf < thr and self._busy_requests(aid) and conf >= thr * 0.5:
            # called while it's talking: its own voice is in the microphone too, so the name scores lower
            # and checking it with speech recognition only hears the agent's own words. Take it.
            log.info("wake '%s' (%.2f) while %s is busy: interrupting", name, conf, aid)
            self._start_listen(source, aid, name, after, threshold, words, name_audio, conf, after_voiced)
            return
        if conf < thr:
            # (a near miss: within a quarter of the threshold. It was half, and every name the partial
            # result glimpsed but the finished phrase didn't have (0.45) opened a listen)
            if conf >= thr * self.NEAR_MISS and self.settings.get("wake_word.verify_near_misses", True) and \
                    self.stt_spells_names(agent):
                # unsure: start listening right away anyway -- checking with speech recognition first took
                # seconds before the listen even began. The name's own audio goes in with the request, and
                # when it's transcribed speech recognition decides (Engine._finish_session): not the name
                # -> nothing happens, and the indicator spins away red.
                log.info("wake '%s' unsure (%.2f < %.2f): listening; speech recognition confirms the name",
                         name, conf, thr)
                self._start_listen(source, aid, name, after, threshold, words, name_audio, conf, after_voiced)
            else:
                log.info("wake '%s' ignored (confidence %.2f < %.2f)", name, conf, thr)
                self.clear_maybe(source)           # (quietly: clearly not a call -- a "juh", a "gee")
            return
        log.info("wake '%s' (%.2f) on %s", name, conf, source)
        self._start_listen(source, aid, name, after, threshold, words, name_audio, conf, after_voiced)

    def _start_listen(self, source: str, aid: str, name: str, after: list[bytes], threshold: float | None,
                      words: int, name_audio: list[bytes] | None = None, conf: float = 1.0,
                      after_voiced: list[bool] | None = None) -> None:
        self.clear_maybe(source)                     # (its own listening indicator takes over)
        if self._hears_itself(aid, name):
            return
        paused = self._interrupt(aid)
        if "stt" in self.models.suspended:
            self.flash_unavailable(aid, f"speech recognition is unloaded while {self.models.suspended['stt']} is open")
        s = self.open_session(source, aid, "extended" if aid in self.extended else "request")
        s.interrupting = paused
        spoke_at, count, ongoing = self._speech_after_name(after, after_voiced, threshold, words)
        if spoke_at is not None:
            # words already followed the name ("Jeeves, open OBS" in one breath)
            s.frames = list(after)
            s.got_speech = True
            s.last_voice = s.first_voice = spoke_at
            s.voiced_frames = s.detected_frames = count
        elif after:
            # not clearly speech yet, but keep the audio: the name is reported as soon as it's heard,
            # so the first syllables of the request can be in here. It doesn't count as you having
            # spoken -- the session still waits up to NO_SPEECH_TIMEOUT for you to start talking.
            s.frames = list(after)[-50:]
            if ongoing:
                # the name is still sounding (the speech detector lags a little, more over music): that
                # isn't the request starting -- unless it goes on into one
                s.tail_until = time.time() + TAIL_LIMIT
        if name_audio:
            s.preroll = list(name_audio)
        s.wake_conf = conf
        s.wake_name = name
        s.wake_words = self._wake_words.pop(source, [])
        if spoke_at is None and self._wake_reply_phrases(self.agents().get(aid, {})) and \
                conf >= self.threshold(self.agents().get(aid, {})):     # (an unsure name gets no "Yes?")
            # still quiet by then: "Yes?" (Listener.process decides, by whether you've started talking --
            # not by the wake model's "words after the name", which a breath or room noise also gives)
            s.reply_at = time.time() + REPLY_WAIT

    def _speech_after_name(self, after: list[bytes], after_voiced: list[bool] | None, threshold: float | None,
                           words: int) -> tuple[float | None, int, bool]:
        """(when the request was last heard in the audio after the name or None, how many frames of it,
        whether the name itself is still sounding at the end of it)."""
        if after_voiced is None or len(after_voiced) != len(after):
            # the level meter. Vosk's end time for the name is often early, so the first ~quarter second
            # after it is mostly the name's own tail ("-ves") -- which counted as the request starting,
            # ended the listen a moment later and got an instant answer to nothing. Judge after that.
            spoke_at, count = self._speech_frames(after[NAME_TAIL_FRAMES:], threshold, words)
            return spoke_at, count, False
        v = list(after_voiced)
        start = 0
        while start < len(v) and v[start]:           # the name's own end, still sounding
            start += 1
        ongoing = bool(v) and start >= len(v)         # (if it's really the request going on, the listen sees)
        idx = [i for i in range(start, len(v)) if v[i]]
        if len(idx) < 250 // FRAME_MS and not (words >= 1 and len(idx) >= 120 // FRAME_MS):
            return None, 0, ongoing
        return time.time() - (len(v) - 1 - idx[-1]) * FRAME_MS / 1000, len(idx), False

    # ------------------------------------------------------------------ wake-up reply ("Yes?")
    def _wake_reply_phrases(self, agent: dict[str, Any]) -> list[str]:
        raw = agent.get("wake_reply") or ""
        items = raw if isinstance(raw, list) else str(raw).split("|")
        return [p.strip() for p in items if p and p.strip()]

    def wake_reply(self, s: Session) -> None:
        """The agent's wake-up reply ("Yes?"), said when it starts listening and you're waiting. The
        listen ignores the audio while it plays (the microphone hears it too)."""
        agent = self.agents().get(s.agent_id or "", {})
        phrases = self._wake_reply_phrases(agent)
        if not phrases or s.ended:
            return
        s.replied = True
        if not self.start_io:
            return                                      # (no audio here: tests)
        s.deaf_until = time.time() + 4.0                # until it has played (set exactly when it's done)
        self.run_async(self._play_wake_reply, s, agent, random.choice(phrases))

    def _play_wake_reply(self, s: Session, agent: dict[str, Any], phrase: str) -> None:
        if muted(agent, "audio"):                     # audio mute: no "Yes?" either
            s.deaf_until = 0.0
            return
        try:
            pcm, rate = self._wake_reply_audio(agent, phrase)
            # it's for you: where you hear it, even if the agent's answers go into the call (Jeeves-Microphone)
            mine = agent if agent.get("output_to", "speakers") in ("speakers", "both", "device") else \
                dict(agent, output_to="speakers")
            pb = Playback(pcm, rate, self.voice_targets(mine))
            log.info("  wake-up reply: %s", phrase)
        except Exception:  # noqa: BLE001 -- never let the reply break listening
            log.exception("wake-up reply failed")
            s.deaf_until = 0.0
            return
        with self._lock:
            self.speaking += 1
        try:
            if not s.ended:
                pb.play()
                if pb.error:
                    log.warning("  couldn't play the wake-up reply: %s", pb.error)
        finally:
            with self._lock:
                self.speaking -= 1
                self.speaking_until = time.time() + 0.3
            s.deaf_until = time.time() + 0.15           # its echo dies away, then the listen starts

    def _wake_reply_audio(self, agent: dict[str, Any], phrase: str) -> tuple[bytes, int]:
        """Synthesized once and kept, so the reply comes instantly."""
        key = (phrase, self.models.model_id("tts", agent), self.models.model_id("tts_voice", agent),
               json.dumps(agent.get("voice_style") or {}, sort_keys=True))
        hit = self._reply_cache.get(key)
        if hit is None:
            hit = self._reply_cache[key] = self._synth(agent, phrase)
        return hit

    def _prewarm_wake_replies(self) -> None:
        for aid, agent in self.agents().items():
            if not agent.get("enabled", True):
                continue
            for phrase in self._wake_reply_phrases(agent):
                key = (aid, phrase, self.models.model_id("tts", agent), self.models.model_id("tts_voice", agent),
                       json.dumps(agent.get("voice_style") or {}, sort_keys=True))
                if key in self._reply_tried:
                    continue
                self._reply_tried.add(key)
                try:
                    self._wake_reply_audio(agent, phrase)
                except Exception:  # noqa: BLE001
                    log.warning("couldn't prepare %s's wake-up reply %r", aid, phrase)

    def _didnt_catch(self, s: Session) -> None:
        """A listen that heard nothing usable: say so on the indicator instead of silently doing nothing."""
        self.set_indicator(s.request_id, s.agent_id, "unclear", "Didn't catch that -- say it again?")
        threading.Timer(3.0, lambda: self.set_indicator(s.request_id, s.agent_id, "idle")).start()

    def _speech_in(self, frames: list[bytes], threshold: float | None = None, words: int = 0) -> float | None:
        """When the last real speech in these frames was (a timestamp), or None if they hold no
        more than noise (by the level meter)."""
        return self._speech_frames(frames, threshold, words)[0]

    def _speech_frames(self, frames: list[bytes], threshold: float | None = None,
                       words: int = 0) -> tuple[float | None, int]:
        """(_speech_in, how many frames were speech). Speech is judged against this audio's own
        background (a quiet mic's words are quiet too): ~0.25 s clearly above it counts, and so do two
        or more words the wake word model heard after the name."""
        from .audio import FRAME_MS, rms
        if not frames:
            return None, 0
        from .listener import speech_threshold
        levels = [rms(f) for f in frames]
        cfg = float(self.settings.get("audio.vad_threshold", 0.012))
        # the listener knows the background from the seconds before the name; else the clip's quietest part
        thr = threshold if threshold is not None else \
            min(cfg, speech_threshold(sorted(levels)[len(levels) // 10], cfg))
        voiced = [i for i, lv in enumerate(levels) if lv > thr]
        # ~0.25 s clearly above the background; or words the wake model heard, if there's some speech too
        if len(voiced) < 250 // FRAME_MS and not (words >= 2 and len(voiced) >= 120 // FRAME_MS):
            return None, 0
        last = voiced[-1] if voiced else len(frames) - 1
        return time.time() - (len(frames) - 1 - last) * FRAME_MS / 1000, len(voiced)

    BACKUP_WORDS = 4            # the name within the first few words: "okay so, Jeeves, ..."

    def on_backup_utterance(self, source: str, pcm: bytes, end_frame: int | None = None) -> None:
        """Something was said and the wake word model heard no name in it. It misses some -- a name run
        together with other words ("hey Jeeves what time is it"), said fast, or over loud sound -- so
        speech recognition checks: a name near the start makes it a call after all."""
        if self.sessions.get(source) is not None or self.answer_pending(source) or self.previewing():
            return
        if not self._might_be_a_call(source, pcm):
            return                                   # nothing like a name in it: Whisper never runs
        # something like a name was in it: the spinner shows while speech recognition checks (it was
        # blank for those seconds -- a call the wake model missed only showed up once it answered)
        guess = self._backup_guess.pop(source, None) or self.main_agent() or next(iter(self.active_agents()), None)
        rid = f"backup-{source}"
        if guess:
            self._checking(rid, guess)
        try:
            self._backup_check(source, pcm, end_frame)
        finally:
            if guess:
                self.set_indicator(rid, guess, "idle")

    def _backup_check(self, source: str, pcm: bytes, end_frame: int | None) -> None:
        try:
            text = self.transcribe(pcm)
        except ModelUnavailable:
            return
        if not text.strip() or is_noise_text(text) or self.sessions.get(source) is not None:
            return
        for aid, a in self.active_agents().items():
            if not self._listens(a, source):
                continue
            found, rest = self.strip_name(text, aid, within=self.BACKUP_WORDS)
            if not found:
                continue
            if self._hears_itself(aid, a.get("name", "") or aid):
                return
            log.info("the wake word model missed a call; speech recognition heard: %s", text)
            paused = self._interrupt(aid)
            if rest and not self._no_request_in(rest):
                if paused and self._after_interruption(paused, rest):
                    return
                self.handle_text(rest, aid, source=f"voice:{source}")
            else:                                  # just the name: listen for the request
                after, voiced = [], None
                lst = self.listeners.get(source)
                if lst is not None and end_frame is not None and 0 < lst.frame_no - end_frame <= len(lst.ring):
                    k = lst.frame_no - end_frame             # what you've said since (while it was checking)
                    after, voiced = list(lst.ring)[-k:], list(lst.voiced_ring)[-k:]
                # (words=1: speech in `after` is the request, not the name's own end)
                self._start_listen(source, aid, "", after, None, 1, after_voiced=voiced)
                s = self.sessions.get(source)
                if s is not None and s.agent_id == aid and not s.got_speech and self._wake_reply_phrases(a):
                    s.reply_at = time.time()          # still quiet: "Yes?" now (it took a moment to notice)
            return

    def _might_be_a_call(self, source: str, pcm: bytes) -> bool:
        """Before Whisper checks something said for a missed name: is there anything like one near its
        start? The small wake model transcribes it roughly (a fraction of a second of one core); only if a
        word there resembles a call name does Whisper -- all the CPU's cores for seconds, every time
        anyone on the desktop or in the room said anything -- get to check. (Listening & Keys >
        wake_word.backup_precheck; off = every utterance, as before.)"""
        if not self.settings.get("wake_word.backup_precheck", True):
            return True
        if source != "microphone" and not self.settings.get("wake_word.backup_desktop", False):
            return False                             # desktop audio: the wake model alone (people in a call...)
        spotter = self.models.wake_spotter()
        if spotter is None:
            return True
        try:
            rough = spotter.quick_text(pcm[: 16000 * 2 * 4])        # the first 4 s: names come first
        except Exception:  # noqa: BLE001 -- can't tell: let Whisper check
            return True
        words = normalize(rough).split()[:6]
        if not words:
            return False
        import difflib
        names = {w for a in self.active_agents().values() for n in [a.get("name", "")] + list(a.get("call_names", []))
                 for w in normalize(n).split() if len(w) >= 3}
        owner = {w: aid for aid, a in self.active_agents().items()
                 for n in [a.get("name", "")] + list(a.get("call_names", [])) for w in normalize(n).split()}
        for w in words + [a + b for a, b in zip(words, words[1:])]:
            for n in names:
                if difflib.SequenceMatcher(None, w, n).ratio() >= 0.6 or w[:3] == n[:3]:
                    self._backup_guess[source] = owner.get(n)
                    return True
        return False

    def on_utterance(self, source: str, pcm: bytes, may_call: bool = True) -> None:
        """Transcribe-everything path: Summary log, audio keyword triggers, and
        call names found in the transcript (may_call: whether a name in it may call an agent --
        hold-to-listen, Listener._may_call)."""
        if self.previewing():
            may_call = False                        # (a voice preview saying a name calls nobody)
        rid, guess = f"heard-{source}", None
        ended = time.time()
        started = ended - len(pcm) / 32000
        if may_call and self.sessions.get(source) is None and self.models.wake_spotter() is not None \
                and not self._woken_during(source, started, ended) and self._might_be_a_call(source, pcm):
            # names come from the transcript here: show the spinner while speech recognition works
            guess = self._backup_guess.pop(source, None) or self.main_agent()
            if guess:
                self._checking(rid, guess)
        try:
            self._on_utterance(source, pcm, may_call, started, ended)
        finally:
            if guess:
                self.set_indicator(rid, guess, "idle")

    def _light_transcripts(self, source: str) -> Any:
        """The wake word model, when only the Summary needs everything transcribed: its rough text is good
        enough for a summary, and costs a fraction of one core -- Whisper large on the CPU took every core
        for seconds each time anyone in the room (or on the desktop) said anything, which is what kept the
        CPU hot while Jeeves sat idle. Keyword triggers and jumping in still get Whisper.
        (Settings: summary.transcriber = light | whisper.)"""
        if self.settings.get("summary.transcriber", "light") != "light":
            return None
        if self.jump_in.wants(source) or any(t.get("event") == "audio_keyword" and
                                             t.get("source", "microphone") in (source, "both")
                                             for t in self.triggers.all()):
            return None
        return self.models.wake_spotter()

    def _checking(self, rid: str, agent_id: str) -> None:
        """While speech recognition checks whether a name was said (a call the wake model may have
        missed): a barely-visible dot by default; the agent's spinner with "Checking what you said"; or
        nothing (Settings > Indicators: indicators.checking = subtle | full | off)."""
        how = self.settings.get("indicators.checking", "subtle")
        if how == "full":
            self.set_indicator(rid, agent_id, "thinking", "Checking what you said")
        elif how != "off":
            self.set_indicator(rid, agent_id, "checking")

    def _woken_during(self, source: str, started: float, ended: float) -> bool:
        """Did the wake word model call an agent while this was being said? Then the call has been handled
        (or is being): the transcript mustn't start it again. Judged by when it was SAID -- Whisper on the
        CPU takes seconds, so "a wake in the last few seconds" (the old check) had usually expired by
        the time the transcript came back, and every call ran twice with Summary on."""
        return any(started - 1.5 <= t <= ended + 1.0 for t in self._wakes.get(source, []))

    def _on_utterance(self, source: str, pcm: bytes, may_call: bool, started: float = 0.0,
                      ended: float = 0.0) -> None:
        if not ended:
            ended = time.time()
            started = ended - len(pcm) / 32000
        spotter = self._light_transcripts(source)
        if spotter is not None:
            try:
                rough = spotter.quick_text(pcm)
            except Exception:  # noqa: BLE001
                rough = ""
            if rough.strip():
                self.summary.add(source, rough)
            # Whisper only for what might be a call (a name-like word near the start, not one the wake
            # model already handled)
            if not may_call or not self.listens_for_names() or self.sessions.get(source) is not None \
                    or self.answer_pending(source) \
                    or self._woken_during(source, started, ended) \
                    or not self._might_be_a_call(source, pcm):
                return
        try:
            text = self.transcribe(pcm)
        except ModelUnavailable:
            return
        if not text.strip() or is_noise_text(text):
            return
        if spotter is None:
            self.summary.add(source, text)
        self.triggers.on_transcript(text, source)
        self.jump_in.heard(source, text)
        if self.sessions.get(source) is not None or self.answer_pending(source):
            return
        by_model = "vosk" in self.detection_modes(source)
        # names in the text call an agent when there's no wake word model -- or, beside it, as the backup
        # for a name it missed (like the backup path, which doesn't run while everything is transcribed)
        wake_by_text = self.listens_for_names() and (not by_model or bool(self.settings.get("wake_word.stt_backup", True)))
        aid, rest, _ = self.split_agent(text, source)
        if aid is None:
            self.jump_in.consider(source)      # nobody was addressed: maybe an agent wants to chime in
            return
        if not wake_by_text or not may_call:   # the wake word model handles addressed requests / keys not held
            return
        if by_model:
            if self._woken_during(source, started, ended) or self._woken_during(source, ended, time.time()):
                return                         # the wake word model heard this call: it's been handled
            found, rest2 = self.strip_name(text, aid, within=self.BACKUP_WORDS)
            if not found:
                return                         # a name mid-sentence is someone being talked about
            rest = rest2
            log.info("the wake word model missed a call; speech recognition heard: %s", text)
        if self._hears_itself(aid, text):
            return
        log.info("  heard (whole-sentence transcription, %s): %s", self._why_modes.get(source, "?"), text)
        paused = self._interrupt(aid)
        if rest:
            if paused and self._after_interruption(paused, rest):
                return
            self.handle_text(rest, aid, source=f"voice:{source}")
        else:
            self.open_session(source, aid, "request").interrupting = paused

    # ------------------------------------------------------------------ calling a busy agent
    def _busy_requests(self, agent_id: str) -> list[str]:
        with self._lock:
            return [rid for rid, c in self.active.items()
                    if c.agent_id == agent_id and not c.dry_run and not c.suspend_event.is_set()
                    and not c.background]

    def _hears_itself(self, agent_id: str, heard: str) -> bool:
        """The microphone picking up an agent's own voice isn't the user calling: an agent saying the
        name right now -- or a word that sounds like it ("achieves", "these", "Jesus"), which the wake
        word model hears as "Jeeves" -- made it stop mid-sentence to listen to itself."""
        h = normalize(heard).split()
        if not h:
            return False
        target, now = h[0], time.time()
        with self._lock:
            spoken = [(c.saying, getattr(c, "saying_since", 0.0), getattr(c, "saying_for", 0.0))
                      for c in self.active.values() if getattr(c, "saying", "")] + list(self._just_said)
        for text, since, dur in spoken:
            words = normalize(text).split()
            if since and dur > 0 and len(words) > 8:
                # about where it is in the sentence (speech is evenly paced): the last ~2.5 s of it
                hi = min(len(words), int((now - since) / dur * len(words)) + 2)
                lo = max(0, int((now - 2.5 - since) / dur * len(words)) - 1)
                words = words[lo:hi]
            if any(w == target or ends_like(w, target) >= 0.6 for w in words):
                return True
        return False

    def video_position(self, position: int) -> bool:
        """The player's position now and then: told to Jellyfin when it's playing something from there."""
        item = self.video_state.get("jellyfin")
        if item:
            self.run_async(lambda: self._jellyfin().report(item, int(position)))
        return True

    def video_closed(self, position: int | None = None) -> bool:
        item = self.video_state.get("jellyfin")
        self.video_state.update(playing=False, jellyfin=None)
        if item and position is not None:
            self.run_async(lambda: self._jellyfin().report(item, int(position), stopped=True))
        return True

    def _jellyfin(self) -> Any:
        from ..functions.partials import jellyfin
        return jellyfin.from_settings(self.settings)

    def video_quality(self, height: int, position: int = 0) -> bool:
        """The player asked for another quality: fetch those streams and carry on from position."""
        page = self.video_state.get("page")
        if not page:
            return False

        def work() -> None:
            from ..functions.partials import youtube as yt
            try:
                s = yt.streams(page, int(height))
            except Exception as exc:  # noqa: BLE001
                self.publish("notice", {"text": f"Couldn't switch the video quality: {exc}"})
                return
            self.publish("video", {"action": "play", "title": s["title"], "channel": s["channel"], "video": s["video"],
                                   "audio": s["audio"], "page": s["page"], "duration": s["duration"],
                                   "height": s.get("height"), "heights": s.get("heights"), "fps": s.get("fps"),
                                   "chapters": s.get("chapters"), "start": int(position), "fullscreen": None})
        self.run_async(work)
        return True

    def interrupt(self, request_id: str) -> bool:
        """Right-click > Interrupt: pause what the agent is doing (speech stops mid-sentence) and listen
        for you right away -- the same as calling it by name. "carry on" resumes, anything else is a new
        request."""
        with self._lock:
            ctx = self.active.get(request_id)
        if ctx is None:
            raise KeyError(request_id)
        if self._refuse_if_off():
            return False
        a = ctx.agent
        source = f"device:{a['listen_device']}" if a.get("listen_to") == "device" and a.get("listen_device") else \
            "desktop" if a.get("listen_to") == "desktop" else "microphone"
        self._start_listen(source, ctx.agent_id, "", [], None, 0)
        self.apply_settings()                    # make sure that source is being listened to
        return True

    def _interrupt(self, agent_id: str) -> list[str]:
        """Called by name while busy: pause what it's doing (speech stops mid-sentence) and listen.
        Returns everything now paused for it, including work still paused from an earlier call."""
        paused = []
        for rid in self._busy_requests(agent_id):
            try:
                if self.suspend(rid):
                    paused.append(rid)
            except KeyError:
                pass
        if paused:
            log.info("%s was called while busy: paused %s", agent_id, paused)
        with self._lock:
            held = self.interrupted.setdefault(agent_id, [])
            held[:] = [r for r in held if r in self.active] + [r for r in paused if r not in held]
            return list(held)

    def _resume(self, request_ids: list[str]) -> None:
        for rid in request_ids:
            ctx = self.active.get(rid)
            if ctx is not None and ctx.suspend_event.is_set():
                try:
                    self.suspend(rid)
                except KeyError:
                    pass
        with self._lock:
            for held in self.interrupted.values():
                held[:] = [r for r in held if r not in request_ids]

    def _after_interruption(self, paused: list[str], text: str) -> bool:
        """What was said after calling a busy agent: "carry on" (or nothing) resumes, "wait" keeps it
        paused, "stop" drops the paused work -- returns True when that was all. Anything else is a
        new request that replaces what it was doing (returns False so it gets handled)."""
        t = re.sub(r"^(?:no|ok|okay|oh|um|uh|sorry)[,.\s]+", "", text.strip().lower()).strip(" .,!?")
        if not t or re.match(r"^(continue|go on|go ahead|carry on|keep going|resume|proceed|never ?mind|nothing|"
                             r"as you were|sorry|that's all|it's fine|you can continue|unpause)\b", t):
            self._resume(paused)
            return True
        if re.match(r"^(wait|hold on|hang on|one (sec|second|moment)|just a (sec|second|moment)|pause)\b", t):
            return True                                    # stays paused until called again
        for rid in paused:
            try:
                self.close_request(rid)
            except KeyError:
                pass
        with self._lock:
            for held in self.interrupted.values():
                held[:] = [r for r in held if r not in paused]
        return bool(re.match(r"^(stop|cancel|shut up|be quiet|quiet|enough|that's enough|abort|forget it|"
                             r"stop talking|hush|silence)\b", t))

    def mic(self, action: str) -> None:
        """Onscreen microphone: click = +5 s, hold = keep listening until release (+1 s)."""
        s = self.sessions.get("microphone")
        if s is None:
            return
        now = time.time()
        if action == "click":
            s.extend_until = max(s.extend_until, now) + float(self.settings.get("general.mic_click_extend_seconds", 5))
        elif action == "hold":
            s.hold = True
        elif action == "release":
            s.hold = False
            s.extend_until = now + float(self.settings.get("general.mic_hold_release_seconds", 1))
        self.set_indicator(s.request_id, s.agent_id, "listening", "held" if s.hold else "",
                           extend_until=s.extend_until, source="microphone", mode=s.mode)

    def voice_request(self, agent_id: str | None) -> str:
        """Manual Voice Request: start listening now for this agent."""
        if self._refuse_if_off():
            raise ValueError("Jeeves is turned off")
        if agent_id in (None, "", "_unknown", "unknown"):
            # Voice Request: Unknown -- listen now; the agent is whoever you name ("Jeeves, ...")
            agent_id = None
        elif agent_id not in self.agents():
            raise ValueError(f"no agent '{agent_id}'")
        if "stt" in self.models.suspended:
            self.flash_unavailable(agent_id, f"speech recognition is unloaded while {self.models.suspended['stt']} is open")
        s = self.open_session("microphone", agent_id, "request")
        s.manual = True                          # (naming nobody: the Main agent, if there is one)
        if agent_id is None:
            main = self.agents().get(self.main_agent() or "", {}).get("name")
            self.set_indicator(s.request_id, None, "listening",
                               f"for {main} (or say another agent's name)" if main else "say an agent's name first",
                               source="microphone", mode="request")
        else:
            self.wake_reply(s)
        self.apply_settings()                    # starts the mic if it wasn't already listening
        return s.request_id

    def training_record(self, text: str) -> str:
        """Record the user reading a training phrase (ends after they stop talking)."""
        if self._refuse_if_off():
            raise ValueError("Jeeves is turned off")
        s = self.open_session("microphone", None, "training")
        s.text = text
        self.apply_settings()
        return s.request_id

    # ------------------------------------------------------------------ extended prompt mode
    def begin_extended(self, agent_id: str) -> None:
        self.extended[agent_id] = {"parts": [], "started": time.time()}
        self.publish("extended", {"agent": agent_id, "on": True})

    def end_phrase(self) -> str:
        f = self.registry.get("extended_prompt_mode")
        kws = self.registry.keywords(f) if f else []
        base = kws[0] if kws else "extended prompt mode"
        return f"end {base.removeprefix('start ')}"

    def extended_chunk_ready(self, s: Session, now: float, eos: float) -> bytes | None:
        if s.got_speech and not s.hold and now - s.last_voice >= eos and now >= s.extend_until:
            pcm = s.pcm()
            s.frames, s.got_speech = [], False
            return pcm
        return None

    def on_extended_chunk(self, s: Session, pcm: bytes) -> None:
        agent = self.agents().get(s.agent_id or "", {})
        try:
            text = self.transcribe(pcm, agent)
        except ModelUnavailable as exc:
            self.flash_unavailable(s.agent_id, exc.reason)
            return
        self._extended_text(s.agent_id, text, s)

    def _extended_text(self, agent_id: str | None, text: str, s: Session | None = None) -> bool:
        """Add text to an agent's extended prompt; returns True if it ended it."""
        if agent_id not in self.extended:
            return False
        end = self.end_phrase()
        low = normalize(text)
        i = low.find(normalize(end))
        state = self.extended[agent_id]
        if i < 0:
            state["parts"].append(text.strip())
            self.publish("extended", {"agent": agent_id, "on": True, "text": " ".join(state["parts"])})
            return False
        before = text[: max(0, len(text) * i // max(1, len(low)))].strip()
        if before:
            state["parts"].append(before)
        full_text = " ".join(p for p in state["parts"] if p)
        del self.extended[agent_id]
        self.publish("extended", {"agent": agent_id, "on": False})
        if s is not None:
            self.end_session(s)
            self.set_indicator(s.request_id, agent_id, "idle")
        if full_text:
            self.handle_text(full_text, agent_id, source="extended", skip_functions={"extended_prompt_mode"})
        return True

    # ------------------------------------------------------------------ waiting for the user
    def answer_pending(self, source: str | None = None) -> str | None:
        """The agent waiting for a spoken answer; with a source, only one that listens to it
        (a desktop-only agent's question doesn't open your microphone)."""
        with self._lock:
            for w in self.waiters.values():
                if w.kind in ("answer", "confirm", "click"):
                    if source is None or self._listens(w.ctx.agent, source) or \
                            w.ctx.entry.get("source") == f"voice:{source}":    # asked where the request came from
                        return w.ctx.agent_id
        return None

    def wait_for_answer(self, ctx: FunctionContext, question: str, choices: list[str] | None,
                        timeout: float) -> str | None:
        w = Waiter("answer", ctx, question, choices)
        return self._wait(ctx, w, timeout, f"{question}", "asking")

    def wait_for_confirm(self, ctx: FunctionContext, text: str, hint: str, timeout: float = 120.0,
                         click_only: bool = False) -> bool:
        w = Waiter("click" if click_only else "confirm", ctx, text)
        return bool(self._wait(ctx, w, timeout, text + (f" — {hint}" if hint else ""), "asking", confirm=True))

    def _wait(self, ctx: FunctionContext, w: Waiter, timeout: float, detail: str, stage: str, **extra: Any) -> Any:
        rid = ctx.entry["id"]
        with self._lock:
            self.waiters[rid] = w
        self.run_async(self.apply_settings)      # make sure the mic is listening for the answer
        ctx.state(stage, detail, waiting=w.kind, choices=w.choices, **extra)
        try:
            deadline = time.time() + timeout
            while not w.event.is_set():
                if ctx.cancel_event.wait(0.1):
                    raise Cancelled()
                if time.time() > deadline:
                    break
            return w.value
        finally:
            with self._lock:
                self.waiters.pop(rid, None)
            self.run_async(self.apply_settings)
            ctx.state("thinking")

    def _deliver_answer(self, agent_id: str | None, text: str) -> None:
        with self._lock:
            waiter = next((w for w in self.waiters.values() if w.ctx.agent_id == agent_id), None)
            if waiter is None:
                waiter = next(iter(self.waiters.values()), None)
        if waiter is None:
            return
        if waiter.kind in ("confirm", "click"):
            keyword = normalize(self.settings.get("run_command.confirm_keyword", "proceed"))
            low = normalize(text)
            if keyword and keyword in low:
                waiter.resolve(True)
            elif re.search(r"\b(no|cancel|stop|don't|do not)\b", low):
                waiter.resolve(False)
            return
        waiter.resolve(text)

    def answer(self, request_id: str | None, text: str | None = None, confirm: bool | None = None) -> bool:
        """From the overlay/GUI: typed answer, or a confirm/deny."""
        with self._lock:
            w = self.waiters.get(request_id) if request_id else next(iter(self.waiters.values()), None)
        if w is None:
            return False
        if confirm is not None and w.kind in ("confirm", "click"):
            w.resolve(bool(confirm))
        elif text is not None and w.kind == "answer":
            w.resolve(text)
        elif text is not None:
            self._deliver_answer(w.ctx.agent_id, text)
        return True

    def suspend(self, request_id: str) -> bool:
        """Right-click > Suspend / Resume. A running request pauses at its next step (speech
        pauses, model output stops streaming); a listening session stops taking audio.
        Returns True when it is now suspended."""
        with self._lock:
            ctx = self.active.get(request_id)
            session = next((s for s in self.sessions.values() if s.request_id == request_id), None)
        if ctx is not None:
            now_suspended = not ctx.suspend_event.is_set()
            if now_suspended:
                ctx.suspend_event.set()
            else:
                ctx.suspend_event.clear()
            if ctx.playback is not None:
                if now_suspended:
                    ctx.playback.paused.set()
                else:
                    ctx.playback.paused.clear()
            ctx.trace("suspended" if now_suspended else "resumed")
            st = self.indicators.get(request_id, {})
            self.set_indicator(request_id, ctx.agent_id, st.get("stage", ctx.stage), st.get("detail", ""))
            return now_suspended
        if session is not None:
            session.suspended = not session.suspended
            if not session.suspended:
                session.started = session.last_voice = time.time()   # fresh silence timer
            self.set_indicator(session.request_id, session.agent_id, "listening",
                               "suspended" if session.suspended else "", source=session.source, mode=session.mode)
            return session.suspended
        raise KeyError(request_id)

    def close_request(self, request_id: str) -> bool:
        """Right-click > Close: stop this one request (or listening session) only."""
        with self._lock:
            ctx = self.active.get(request_id)
            session = next((s for s in self.sessions.values() if s.request_id == request_id), None)
            if session is not None:
                self.sessions.pop(session.source, None)
        if ctx is not None:
            ctx.suspend_event.clear()
            ctx.cancel()
        if session is not None:
            session.ended = True
            self.run_async(self.apply_settings)
        self.set_indicator(request_id, ctx.agent_id if ctx else (session.agent_id if session else None), "idle")
        return ctx is not None or session is not None

    def indicator_click(self, request_id: str) -> str:
        """Clicking an indicator: responding -> pause/resume, asking -> confirm
        (or open the browser), thinking/researching -> the overlay shows thoughts."""
        with self._lock:
            ctx = self.active.get(request_id)
            w = self.waiters.get(request_id)
        if w is not None and w.kind in ("confirm", "click"):
            w.resolve(True)
            return "confirmed"
        if ctx is not None and ctx.stage == "responding" and ctx.playback is not None:
            paused = ctx.playback.toggle_pause()
            self.set_indicator(request_id, ctx.agent_id, "responding", "paused" if paused else "", paused=paused)
            return "paused" if paused else "resumed"
        return "thoughts"

    def thoughts(self, request_id: str) -> dict[str, Any]:
        ctx = self.active.get(request_id)
        if ctx is None:
            entry = self.history.get(request_id) or {}
            return {"thoughts": [], "kinds": [], "looking_at": "", "trace": entry.get("trace", []),
                    "text": entry.get("text", ""), "source": entry.get("source", "")}
        return {"thoughts": ctx.thoughts, "kinds": list(getattr(ctx, "thought_kinds", [])),
                "looking_at": ctx.looking_at, "trace": ctx.entry["trace"],
                "text": ctx.entry.get("text", ""), "source": ctx.entry.get("source", "")}

    # ------------------------------------------------------------------ requests
    def main_agent(self) -> str | None:
        """The Main agent (right-click an agent > Main agent): who a manual request goes to when it
        doesn't name one."""
        m = self.settings.get("general.main_agent", "") or ""
        a = self.agents().get(m)
        return m if a is not None and a.get("enabled", True) else None

    def handle_text(self, text: str, agent_id: str | None = None, source: str = "text",
                    request_id: str | None = None, skip_functions: set[str] | None = None,
                    dry_run: bool = False, wait: bool = False, manual: bool = False) -> dict[str, Any]:
        text = text.strip()
        if not dry_run and self._refuse_if_off():
            return {"error": "Jeeves is turned off"}
        if agent_id is None:
            heard_on = source.split(":", 1)[1] if source.startswith("voice:") else None
            agent_id, rest, _ = self.split_agent(text, heard_on)
            if agent_id is None and (manual or source.split(":", 1)[0] in ("text", "cli", "dry_run")) and \
                    self.main_agent():
                agent_id, rest = self.main_agent(), text      # a manual request naming nobody: the Main agent
            if agent_id is None:
                if dry_run:
                    raise ValueError("start with an agent's name, e.g. 'Jeeves, set a timer for 5 minutes'")
                heard = f" (heard: “{text}”)" if source.startswith("voice") else ""
                self.publish("notice", {"text": "Start with an agent's name, e.g. “Jeeves, set a timer for "
                                                f"5 minutes”{heard}."})
                return {"error": "no agent named in the request"}
            text = rest or text
        agent = self.agents().get(agent_id)
        if agent is None:
            raise ValueError(f"no agent '{agent_id}'")
        if agent_id in self.extended and not dry_run and source != "extended":
            self._extended_text(agent_id, text)
            return {"extended": True}
        # an answer to a pending question takes priority over a new request
        if not dry_run and source.startswith("text") and any(w.ctx.agent_id == agent_id for w in self.waiters.values()):
            self._deliver_answer(agent_id, text)
            return {"answered": True}
        if not dry_run:
            same = self._same_request(agent_id, text)
            if same is not None:                 # heard twice (the wake model and the backup check): once is enough
                log.info("'%s' for %s is already being handled", text, agent_id)
                return {"id": same, "agent": agent_id, "entry": None, "duplicate": True}
        entry = new_entry(text, agent_id, source)
        if request_id:
            entry["id"] = request_id
        entry["dry_run"] = dry_run
        entry["manual"] = manual
        self.history.add(entry)
        ctx = FunctionContext(self, agent_id, agent, entry, dry_run=dry_run)
        if not dry_run:
            # one request at a time per agent: two of them talking over each other, asking two questions
            # at once and fighting over the mouse was the trouble. Something you asked for now replaces
            # what it was doing; a timer, trigger or handoff waits for it to finish (_run).
            if self._user_source(source):
                for rid in self._agent_requests(agent_id):
                    log.info("new request for %s: stopping the one it was doing", agent_id)
                    try:
                        self.close_request(rid)
                    except KeyError:
                        pass
            with self._lock:
                self.active[entry["id"]] = ctx
            if not ctx.background:
                ctx.state("thinking")            # the spinner from the moment it's understood
        if wait or dry_run:
            self._run(ctx, skip_functions or set())
        else:
            self.run_async(self._run, ctx, skip_functions or set())
        return {"id": entry["id"], "agent": agent_id, "entry": entry if (wait or dry_run) else None}

    @staticmethod
    def _user_source(source: str) -> bool:
        return source.split(":", 1)[0] in ("text", "voice", "manual", "review", "extended", "dry_run", "cli", "queued")

    def _agent_requests(self, agent_id: str, exclude: str | None = None) -> list[str]:
        """The agent's requests in progress (not background ones like screen watching)."""
        with self._lock:
            return [rid for rid, c in self.active.items() if c.agent_id == agent_id and rid != exclude
                    and not c.dry_run and not c.background]

    def _same_request(self, agent_id: str, text: str) -> str | None:
        t, now = normalize(text), time.time()
        with self._lock:
            for rid, c in self.active.items():
                if c.agent_id == agent_id and not c.dry_run and normalize(c.entry.get("text", "")) == t and \
                        now - c.entry.get("time", 0) < 6:
                    return rid
        return None

    def _wait_turn(self, ctx: FunctionContext, limit: float = 120.0) -> None:
        """A timer, trigger or handoff for a busy agent: wait until it has finished what it's doing."""
        deadline = time.time() + limit
        while self._agent_requests(ctx.agent_id, exclude=ctx.entry["id"]) and time.time() < deadline:
            ctx.check_cancelled()
            time.sleep(0.2)

    def _decide(self, ctx: FunctionContext, text: str, skip: set[str]) -> Decision:
        agent = dict(ctx.agent)
        if skip:
            agent["functions"] = dict(agent.get("functions") or {}, **{s: False for s in skip})
        return self.intent.decide(agent, text, ctx)

    def _run(self, ctx: FunctionContext, skip: set[str]) -> None:
        entry = ctx.entry
        text = entry["text"]
        try:
            if not ctx.dry_run and not self._user_source(entry.get("source", "text")):
                self._wait_turn(ctx)
            ctx.state("thinking")
            try:
                decision = self._decide(ctx, text, skip)
            except ModelUnavailable as exc:
                self.flash_unavailable(ctx.agent_id, exc.reason)
                if exc.queueable and not ctx.dry_run:
                    self.queue.append(("text", {"text": text, "agent": ctx.agent_id, "skip": list(skip)}))
                    entry["status"] = "queued"
                    ctx.trace("queued", reason=exc.reason)
                    return
                raise FunctionError(exc.reason) from exc
            ctx.trace("intent", **decision.to_dict())
            if self._misfire(ctx, text, decision):
                entry["function"], entry["status"] = None, "ignored"
                ctx.trace("ignored", reason="not meant for the agent")
                log.info("  '%s' wasn't meant for %s (talking to someone else?): ignored", text, ctx.agent_id)
                return
            if decision.refusal:
                entry["function"], entry["status"] = None, "refused"
                entry["response"] = decision.refusal
                if not ctx.dry_run:
                    ctx.say(decision.refusal)
                return
            threshold = float(self.settings.get("general.unclear_confidence", 0.45))
            if decision.function is not None and decision.confidence < threshold:
                ctx.trace("low_confidence", confidence=decision.confidence)    # act on the best guess anyway
            if decision.function is None and not self._worth_asking(decision.question, text) and \
                    self.registry.rudimentary(ctx.agent) is None:      # (a rudimentary agent only asks)
                decision = self._best_guess(ctx, text, skip)                    # answer instead of asking
                ctx.trace("intent", **decision.to_dict())
            rounds = 0
            while decision.function is None and rounds < 2:
                rounds += 1
                question = decision.question or self._clarifying_question(ctx, text)
                ctx.state("unclear", question)
                if ctx.dry_run:
                    entry["function"] = None
                    entry["response"] = f"(would ask) {question}"
                    entry["status"] = "unclear"
                    return
                answer = ctx.ask(question, timeout=45)
                if not answer:
                    ctx.say("Never mind, then.")
                    entry["status"] = "unclear"
                    return
                text = f"{text} ({answer})"
                decision = self._decide(ctx, text, skip)
                ctx.trace("intent", **decision.to_dict())
            if decision.function is None:
                entry["status"] = "unclear"
                ctx.say("Sorry, I still don't follow.")
                return
            entry["function"], entry["args"], entry["confidence"] = decision.function, decision.args, decision.confidence
            if decision.method != "model":           # (the model's own choice is in the thoughts as it wrote it)
                ctx.think(json.dumps({"function": decision.function, "args": decision.args,
                                      "chosen by": {"rules": "a rule (no model needed)", "keywords": "keywords",
                                                    "fallback": "best guess"}.get(decision.method, decision.method)},
                                     ensure_ascii=False, default=str), kind="intent")
            self.publish("request", {"id": entry["id"], "function": decision.function, "args": decision.args})
            if ctx.dry_run:
                self._dry_run_body(ctx, decision)
                entry["status"] = "dry_run"
                return
            result = ctx.call(decision.function, **decision.args)
            mem_kind, cleaned = Memory.detect(entry["text"])
            if mem_kind and decision.function != "remember" and agent_memory(ctx.agent, self.settings)["enabled"]:
                self.memory.add(cleaned, permanent=mem_kind == "permanent", agent=ctx.agent_id)
                ctx.trace("remembered", kind=mem_kind, text=cleaned)
            if not ctx.spoke and result not in (None, "") and not isinstance(result, bool):
                shown = result if isinstance(result, str) else json.dumps(result, default=str, indent=1)
                entry["response"] = shown
                ctx.show(shown)
            entry["status"] = "done"
        except Cancelled:
            entry["status"] = "aborted"
            ctx.trace("aborted")
        except FunctionError as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            ctx.trace("error", message=str(exc))
            if not ctx.dry_run:
                try:
                    ctx.say(f"Sorry, {exc}")
                except Cancelled:
                    pass
        except Exception as exc:
            log.exception("request failed")
            entry["status"] = "error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            ctx.trace("error", message=entry["error"])
            if not ctx.dry_run:
                self.publish("notice", {"text": f"{ctx.agent.get('name')}: something went wrong ({exc})"})
                if not ctx.spoke:                # a spoken request deserves a spoken answer, even this one
                    try:
                        ctx.say("Sorry, something went wrong there.")
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            entry["duration"] = round(time.time() - entry["time"], 3)
            with self._lock:
                self.active.pop(entry["id"], None)
                self.waiters.pop(entry["id"], None)
            if not ctx.dry_run:
                self.set_indicator(entry["id"], ctx.agent_id, "idle")
                self.history.finish(entry)
                self.publish("history", {"entry": entry})

    def _dry_run_body(self, ctx: FunctionContext, decision: Decision) -> None:
        f = self.registry.get(decision.function, ctx.agent)
        ctx.trace("would_call", function=decision.function, args=decision.args)
        if f is None:
            return
        if f.steps is not None or f.kind == "full":
            try:
                result = ctx.call(decision.function, **decision.args)
                ctx.trace("dry_result", result=str(result)[:500])
            except FunctionError as exc:
                ctx.trace("error", message=str(exc))

    def _misfire(self, ctx: FunctionContext, text: str, decision: Decision) -> bool:
        """A wake word that wasn't meant for the agent (Listening & Keys > Ignore talk that isn't for an
        agent): the intent model says so, or it reads like conversation with someone else. Only for
        requests a wake word started -- typed and manual ones are always meant."""
        src = str(ctx.entry.get("source", ""))
        if not src.startswith("voice:") or ctx.entry.get("manual") or src == "voice:queued":
            return False
        if not self.settings.get("general.ignore_chatter", True):
            return False
        from .intent import looks_like_chatter
        # How sure it is that this IS a request: the intent model's own say when it calls the words not
        # meant for the agent (1 - how sure it is of that), or a fair doubt when they only read like chatter.
        # Below "Confidence required to do something" (general.act_confidence) it's ignored. 0.8 is how it
        # always behaved; lower it and it acts on more of what it isn't sure about.
        if decision.not_for_me:
            sure = 1.0 - max(0.0, min(1.0, float(decision.confidence or 0.7)))
        elif decision.function in (None, "local_response") and looks_like_chatter(text):
            sure = 0.3
        else:
            return False
        need = float(self.settings.get("general.act_confidence", 0.8))
        ctx.trace("request_confidence", confidence=round(sure, 2), required=need)
        if sure >= need:
            ctx.think(f"Not sure this was for me ({sure:.2f}), but that's enough (needs {need:.2f}): doing it")
            return False
        return True

    def _worth_asking(self, question: str, text: str) -> bool:
        """A clarifying question is only worth it when it asks for something new -- not when it
        restates the request ("Do you want me to look up X?") or is empty."""
        q, t = normalize(question or ""), normalize(text)
        if not q:
            return False
        if similarity(q, t) > 0.7:
            return False
        generic = {"do", "you", "want", "me", "to", "would", "like", "should", "could", "can", "i", "the", "a",
                   "that", "this", "it", "is", "are", "mean", "exactly", "which", "what", "something", "specific",
                   "sure", "correct", "right", "so", "just", "please", "confirm", "asking", "about", "for", "of"}
        new = set(q.split()) - set(t.split()) - generic
        return bool(new)                   # it asks for something the request didn't already say

    def _best_guess(self, ctx: FunctionContext, text: str, skip: set[str]) -> Decision:
        """No clear function: research it if it's a factual question, otherwise just answer."""
        functions = [f for f in self.registry.enabled_for(ctx.agent) if f.name not in skip]
        enabled = {f.name for f in functions}
        core = self.intent.strip_address(ctx.agent, text)
        by_keywords = self.intent.keyword_decide(ctx.agent, text, functions)
        if by_keywords.function not in (None, "local_response"):      # "set a timer", "look up ..."
            by_keywords.method = "fallback"
            return self.intent.with_said(by_keywords, ctx.agent, text)
        if "research" in enabled and looks_factual(core, ctx.agent):
            return self.intent.with_said(Decision("research", {"depth": "quick"}, 0.6, "", "fallback"),
                                         ctx.agent, text)
        if "local_response" in enabled:
            return self.intent.with_said(Decision("local_response", {}, 0.5, "", "fallback"), ctx.agent, text)
        return Decision(None, {}, 0.0, "", "fallback")

    def _clarifying_question(self, ctx: FunctionContext, text: str) -> str:
        try:
            q = self.models.respond(ctx.agent, f"The user said: \"{text}\". You couldn't tell what they want. "
                                    "Ask ONE short clarifying question.", ctx=None)
        except ModelUnavailable:
            q = None
        return q or "Sorry, what would you like me to do?"

    def dry_run(self, text: str, agent_id: str | None = None) -> dict[str, Any]:
        res = self.handle_text(text, agent_id, source="dry_run", dry_run=True)
        return res.get("entry") or res

    # ------------------------------------------------------------------ speech output
    def speak(self, ctx: FunctionContext, text: str) -> None:
        agent = ctx.agent
        labels = [n for n in [agent.get("name", ""), ctx.agent_id or "", "assistant"] if n]
        text = re.sub(r"^\s*(?:%s)\s*(?:\(you\))?\s*:\s*" % "|".join(re.escape(n) for n in labels), "", text,
                      flags=re.I) or text                # "Jeeves: Certainly" -> "Certainly"
        text = speakable(text) or text               # no "asterisk asterisk", URLs or [2] read out
        self.jump_in.spoke(ctx.agent_id, text)       # part of the conversation even if TTS fails
        if muted(agent, "audio"):
            ctx.trace("muted")                       # audio mute: never speaks (on screen only)
            return
        # a long answer starts with its first sentence while the rest is still being synthesized
        parts = speech_chunks(text) if self.settings.get("audio.stream_speech", True) else [text]
        t_synth = time.time()
        try:
            pcm, rate = self._synth(agent, parts[0])
        except Exception as exc:   # speech failing must never lose the answer
            ctx.trace("tts_failed", reason=str(exc))
            self._tts_problem(f"Text to speech failed: {exc}")
            return
        first_wait = time.time() - t_synth
        if len(parts) > 1:
            pb = Playback(pcm, rate, self.voice_targets(agent), more_coming=True)
            first_seconds = len(pcm) / (2 * rate)

            def rest() -> None:
                try:
                    for n, part in enumerate(parts[1:], 1):
                        if pb.stopped.is_set():
                            break
                        more, r = self._synth(agent, part)
                        pb.add(more if r == rate else resample(more, r, rate))
                        left = sum(len(p) for p in parts[n + 1:])
                        done_chars = max(1, sum(len(p) for p in parts[:n + 1]))
                        ctx.saying_for = pb.seconds() * (1 + left / done_chars)
                except Exception as exc:  # noqa: BLE001 -- what was made so far is still said
                    ctx.trace("tts_failed", reason=str(exc), part="rest")
                    self._tts_problem(f"Text to speech failed partway through: {exc}")
                finally:
                    pb.complete()
            threading.Thread(target=rest, daemon=True, name="speech-rest").start()
        else:
            pb = Playback(pcm, rate, self.voice_targets(agent))
        peak = pb.peak()
        if peak < 40 and text.strip():               # the voice came out silent: say it in eSpeak NG instead
            log.warning("speech for %r came out silent (peak %d); using eSpeak NG", text[:60], peak)
            ctx.trace("speech_silent", peak=peak)
            try:
                from ..models import voicefx
                from ..models.backends import EspeakTTS
                if len(parts) > 1:
                    pb.stop()                        # (ends the first-sentence playback's background work)
                    parts = [text]
                pcm, rate = EspeakTTS().synth(text, None, voicefx.style_of(agent))
                pb = Playback(pcm, rate, self.voice_targets(agent))
            except Exception as exc:  # noqa: BLE001
                self._tts_problem(f"The voice came out silent and eSpeak NG failed too: {exc}")
        log.info("  speaking %.1f s to %s (peak %d)", pb.seconds(), ", ".join(t or "default output"
                                                                              for t in pb.targets), pb.peak())
        ctx.trace("speech", seconds=round(pb.seconds(), 2), targets=pb.targets, peak=pb.peak(),
                  synth_s=round(first_wait, 2), parts=len(parts))
        ctx.playback = pb
        ctx.saying = text
        ctx.saying_since, ctx.saying_for = time.time(), len(pcm) / (2 * rate)
        if len(parts) > 1:                           # (an estimate until the rest is made)
            ctx.saying_for = first_seconds * len(text) / max(1, len(parts[0]))
        with self._lock:
            self.speaking += 1          # desktop listening ignores Jeeves' own voice meanwhile
        try:
            pb.play()
        finally:
            ctx.playback = None
            ctx.saying = ""
            with self._lock:
                self.speaking -= 1
                self.speaking_until = time.time() + 0.5
                # what it just finished saying can still be in the wake word model's last phrase
                self._just_said = [(t, a, d) for t, a, d in self._just_said if a + d > time.time() - 2] + \
                    [(text, ctx.saying_since, ctx.saying_for)]
        if pb.error:
            ctx.trace("playback_failed", reason=pb.error)
            self._tts_problem(f"Couldn't play speech: {pb.error}")
        if pb.stopped.is_set():
            log.info("  speech stopped early (the request was closed or replaced)")
            ctx.trace("speech_stopped")
        ctx.check_cancelled()

    def in_character(self, ctx: FunctionContext, line: str) -> str:
        """A function's plain line ("Timer set for 2:00.") in the agent's own voice ("By my hourglass, two
        minutes and not a grain more!"): every fact, number and name kept -- if the rewrite drops one, or
        no model answers quickly, the plain line is said. Off per agent (Personality > Every line in
        character), for agents without a personality, and for muted voices that say nothing anyway."""
        from ..models import persona
        agent = ctx.agent
        if not persona.has_persona(agent) or not agent.get("persona_every_line", True) or \
                not self.settings.get("persona.every_line", True) or (muted(agent, "audio") and muted(agent, "visual")):
            return line
        if len(line) > 400:
            return line
        import hashlib
        import random
        import re as _re
        key = hashlib.sha1(f"{ctx.agent_id}\0{agent.get('prompt', '')}\0{line}".encode()).hexdigest()
        cache = self.__dict__.setdefault("_character_lines", {})
        known = cache.get(key) or []
        if len(known) >= CHARACTER_VARIANTS:         # said often ("Done.", "Paused."): no model call
            ctx.trace("in_character", line=line, cached=True)
            return random.choice(known)
        # a model too slow for it (CPU-only, busy GPU) says the plain line rather than hold up every reply;
        # it's tried again now and then, so it comes back when the model is quick again
        budget = float(self.settings.get("persona.every_line_max_seconds", 2.5) or 0)
        took = self.__dict__.setdefault("_character_took", [])
        if budget > 0 and len(took) >= 2 and sorted(took[-3:])[len(took[-3:]) // 2] > budget:
            self._character_skips = getattr(self, "_character_skips", 0) + 1
            if self._character_skips % CHARACTER_RETRY_EVERY:
                ctx.trace("in_character_skipped", line=line, recent_s=[round(t, 1) for t in took[-3:]],
                          budget_s=budget)
                return random.choice(known) if known else line
        t0 = time.time()
        try:
            out = self.models.respond(
                agent, line, system=(
                    "Say the line below as yourself, in your own character and manner of speaking -- the way you "
                    "would put it. Keep every fact exactly: each number, time, name and title stays word for word. "
                    "Same meaning, about the same length (one or two sentences). Reply with the line only."),
                ctx=None, raw=False, temperature=0.8, max_tokens=90, kind="step")
        except Exception:  # noqa: BLE001 -- no model: the plain line
            return line
        took.append(time.time() - t0)
        del took[:-6]
        out = (out or "").strip().strip('"“”')
        if not out or len(out) > max(160, len(line) * 4):
            return line
        need = set(_re.findall(r"\d+(?::\d+)?", line))
        if need and not need <= set(_re.findall(r"\d+(?::\d+)?", out)):
            ctx.trace("in_character_dropped_facts", line=line, rewrite=out)
            return line                              # a number went missing: the plain line is right
        if out not in known:
            cache[key] = known + [out]
            while len(cache) > 400:
                cache.pop(next(iter(cache)))
        ctx.trace("in_character", line=line, took_s=round(took[-1], 2))
        return out

    def previewing(self) -> bool:
        """A voice preview is playing (or just finished): no agent wakes meanwhile."""
        return time.time() < getattr(self, "_preview_until", 0.0)

    def lobotomize(self, agent_id: str) -> dict[str, Any]:
        """Start an agent afresh: what it was doing stops, and everything it remembers goes -- its notes,
        its requests (the conversation it would look back on, and their ratings), what it said in the
        conversation Jump in follows, a screen watch, extended prompt mode and its prepared replies. Its
        settings (name, voice, personality, functions) stay."""
        agent = self.agents().get(agent_id)
        if agent is None:
            raise ValueError(f"no agent '{agent_id}'")
        with self._lock:
            ctxs = [c for c in self.active.values() if c.agent_id == agent_id]
            sessions = [s for s in self.sessions.values() if s.agent_id == agent_id]
        for c in ctxs:
            c.cancel()
        for s in sessions:
            s.ended = True
            self.set_indicator(s.request_id, agent_id, "idle")
        self.extended.pop(agent_id, None)
        self.watchers.pop(agent_id, None)
        notes = self.memory.forget_agent(agent_id)
        requests = self.history.forget_agent(agent_id)
        name = agent.get("name", agent_id)
        lines = getattr(self.jump_in, "lines", None)
        if lines is not None:
            keep = [ln for ln in lines if ln[1] != name and ln[3] != f"agent:{agent_id}"]
            lines.clear()
            lines.extend(keep)
        self._reply_cache.clear()
        self._just_said = []
        self.publish("history", {"forgot": agent_id})
        log.info("lobotomized %s: %d notes, %d requests forgotten", agent_id, notes, requests)
        return {"notes": notes, "requests": requests, "stopped": len(ctxs)}

    def speak_test(self, agent_id: str | None = None, text: str = "") -> dict[str, Any]:
        """`jeeves speak-test`: say something the way an answer is said, and report every step."""
        aid = agent_id or self.main_agent() or next(iter(self.agents()), None)
        agent = self.agents().get(aid or "")
        if agent is None:
            raise ValueError("no such agent")
        text = text or f"This is {agent.get('name', aid)}, testing my voice."
        out: dict[str, Any] = {"agent": aid, "text": text, "output_to": agent.get("output_to", "speakers"),
                               "targets": self.voice_targets(agent)}
        try:
            tts, voice = self.models.tts(agent)
            out["tts"] = f"{type(tts).__name__} {getattr(voice, 'id', '') or ''}".strip()
        except Exception as exc:  # noqa: BLE001
            out["tts"] = f"unavailable: {exc}"
        t0 = time.time()
        try:
            pcm, rate = self._synth(agent, text)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"text to speech failed: {exc}"
            return out
        out["synth_seconds"] = round(time.time() - t0, 2)
        pb = Playback(pcm, rate, out["targets"])
        out.update(seconds=round(pb.seconds(), 2), rate=rate, peak=pb.peak())
        from .audio import _player
        out["player"] = " ".join(_player(True, rate, out["targets"][0] if out["targets"] else ""))
        with self._lock:
            self.speaking += 1
        try:
            pb.play()
        finally:
            with self._lock:
                self.speaking -= 1
                self.speaking_until = time.time() + 0.5
        out["played"] = pb.error is None and not pb.stopped.is_set()
        if pb.error:
            out["error"] = f"the player failed: {pb.error}"
        return out

    def test_virtual_mic(self) -> dict[str, Any]:
        """Says a sentence into Jeeves-Microphone only and listens to it at the same time:
        proves the path apps record from, and lets friends in a call hear the test."""
        from .audio import RATE, VIRTUAL_MIC_SOURCE, ensure_virtual_mic, rms
        sink = self.settings.get("audio.virtual_mic_sink", "jeeves-mic")
        if not ensure_virtual_mic(sink, bool(self.settings.get("audio.virtual_mic_include_mic", True)),
                                  self.settings.get("audio.microphone", "")):
            raise RuntimeError("pactl isn't available, so the virtual microphone can't be created")
        source = VIRTUAL_MIC_SOURCE.format(sink=sink)
        agent = next(iter(self.agents().values()), {})
        pcm, rate = self._synth(agent, "This is Jeeves. If you can hear me, the Jeeves microphone works.")
        rec = subprocess.Popen(["parec", "--format=s16le", f"--rate={RATE}", "--channels=1",
                                f"--device={source}"] if shutil.which("parec") else
                               ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1",
                                "--target", source, "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: rec.stdout.read(3200), b"")),
                                  daemon=True)
        reader.start()
        time.sleep(0.3)
        pb = Playback(pcm, rate, [sink])
        with self._lock:
            self.speaking += 1
        try:
            pb.play()
        finally:
            with self._lock:
                self.speaking -= 1
        time.sleep(0.3)
        rec.terminate()
        reader.join(2)
        data = b"".join(chunks)
        frames = [data[i:i + 960] for i in range(0, len(data) - 960, 960)]
        peak = max((rms(f) for f in frames), default=0.0)
        ok = pb.error is None and peak > 0.01
        return {"ok": ok, "source": source, "level": round(peak, 3), "error": pb.error,
                "detail": "Your friends should have heard the test sentence if Discord uses Jeeves-Microphone."
                if ok else "Jeeves played the sentence but nothing came out of Jeeves-Microphone."}

    def voice_targets(self, agent: dict[str, Any]) -> list[str]:
        """Where this agent's voice plays (Speaks through)."""
        return output_targets(agent.get("output_to", "speakers"), self.settings.get("audio.speaker", ""),
                              self.settings.get("audio.virtual_mic_sink", "jeeves-mic"),
                              bool(self.settings.get("audio.virtual_mic_include_mic", True)),
                              self.settings.get("audio.microphone", ""), agent.get("output_device", ""))

    def _synth(self, agent: dict[str, Any], text: str) -> tuple[bytes, int]:
        """The agent's TTS in its voice style; if that fails, eSpeak NG so there's always a voice."""
        from ..models import voicefx
        style = voicefx.style_of(agent)
        try:
            tts, voice = self.models.tts(agent)
            pcm, rate = tts.synth(text, voice, style)
            pitch_done = voice is not None and voice.engine == "espeak-ng"
        except Exception as exc:
            from ..models.backends import EspeakTTS
            log.warning("TTS failed (%s); falling back to eSpeak NG", exc)
            self._tts_problem(f"Text to speech: {exc} -- using eSpeak NG instead")
            pcm, rate = EspeakTTS().synth(text, None, style)
            pitch_done = True
        return voicefx.apply(pcm, rate, style, pitch_done=pitch_done), rate

    def preview_voice(self, agent: dict[str, Any], text: str = "") -> dict[str, Any]:
        """Agents page > Preview: say a sample in this (possibly unsaved) agent's voice, where it speaks
        (Speaks through) -- a specific device or Jeeves-Microphone included."""
        name = agent.get("name") or "your assistant"
        pcm, rate = self._synth(agent, text or f"Hello, I'm {name}. This is how I sound.")
        pb = Playback(pcm, rate, self.voice_targets(agent))
        # nothing wakes while a preview plays (it says the agent's name, and may be routed into the mic)
        self._preview_until = time.time() + pb.seconds() + 2.0
        with self._lock:
            self.speaking += 1
        try:
            pb.play()
        finally:
            with self._lock:
                self.speaking -= 1
                self.speaking_until = time.time() + 0.5
            self._preview_until = time.time() + 1.0
        return {"ok": pb.error is None, "error": pb.error, "seconds": round(len(pcm) / 2 / rate, 1)}

    def voice_speakers(self, voice_id: str) -> dict[str, Any]:
        """Speakers of a multi-speaker voice, with accent/character labels where known."""
        from ..models import catalog
        from ..models.backends import speaker_map
        from ..models.download import is_installed
        v = catalog.get(voice_id)
        if v is None or v.engine != "piper":
            return {"speakers": [], "count": 0, "installed": False}
        base = catalog.get(v.shares) if v.shares else v
        family = (base.voice or "").split("-")[1] if base and base.voice and "-" in base.voice else ""
        labels = catalog.SPEAKER_LABELS.get(family, {})
        m = speaker_map(base) if is_installed(base) else {}
        names = sorted(m, key=lambda k: m[k]) if m else list(labels)
        return {"installed": is_installed(base), "count": max(base.speakers, len(m)),
                "speakers": [{"name": n, "id": m.get(n), "label": labels.get(n, labels.get(n.lower(), ""))}
                             for n in names]}

    def _tts_problem(self, text: str) -> None:
        # once per distinct problem, so a broken voice doesn't spam a notice per sentence
        if text != getattr(self, "_last_tts_problem", None):
            self._last_tts_problem = text
            log.warning("%s", text)
            self.publish("notice", {"text": text})

    # ------------------------------------------------------------------ abort
    def abort(self) -> dict[str, Any]:
        with self._lock:
            ctxs = list(self.active.values())
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.extended.clear()
            self.queue.clear()
        for c in ctxs:
            c.cancel()
        for s in sessions:
            s.ended = True
            self.set_indicator(s.request_id, s.agent_id, "idle")
        self.control.release_all()
        from .audio import stop_all
        stop_all()                                  # every voice too: wake-up replies, timers, previews
        self.publish("abort", {"stopped": len(ctxs)})
        return {"stopped": len(ctxs), "listening_stopped": len(sessions)}

    # ------------------------------------------------------------------ timers/triggers/queue
    def _timer_fired(self, t: dict[str, Any]) -> None:
        if t.get("request"):
            self.handle_text(t["request"], t.get("agent"), source="schedule")
            return
        from ..functions.partials.system import notify
        label = t.get("label") or "Timer"
        notify(f"{label} done", "", app="Jeeves")
        from .audio import play_file, theme_sound
        snd = theme_sound("alarm-clock-elapsed") or theme_sound("complete")
        if snd:
            play_file(snd, self.settings.get("audio.speaker", ""))
        agent_id = t.get("agent") or next(iter(self.agents()), None)
        if agent_id and self.is_on():        # off: the sound and notification only, no voice
            entry = new_entry(f"[timer] {label}", agent_id, "timer")
            ctx = FunctionContext(self, agent_id, self.agents()[agent_id], entry)
            self.run_async(lambda: (ctx.say(f"{label} is done."), self.set_indicator(entry["id"], agent_id, "idle")))

    def _trigger_fired(self, t: dict[str, Any]) -> None:
        agent_id = t.get("agent") or next(iter(self.agents()), None)
        if t.get("steps") is not None and agent_id:
            entry = new_entry(f"[trigger] {t.get('event')} {t.get('target')}", agent_id, "trigger")
            ctx = FunctionContext(self, agent_id, self.agents()[agent_id], entry)
            from ..functions.composer import run_steps

            def go() -> None:
                try:
                    run_steps(t["steps"], ctx, dict(t.get("variables") or {}, event=t.get("detail")))
                except (FunctionError, Cancelled) as exc:
                    log.info("trigger steps stopped: %s", exc)
                finally:
                    self.set_indicator(entry["id"], agent_id, "idle")
            self.run_async(go)
        elif t.get("request"):
            self.handle_text(t["request"], agent_id, source="trigger")

    def _model_back(self, kind: str) -> None:
        if not self.is_on():
            return
        with self._lock:
            pending, self.queue = self.queue, []
        for kind_, item in pending:
            if kind_ == "voice":
                agent = self.agents().get(item["agent"] or "", {})
                try:
                    text = self.transcribe(item["pcm"], agent)
                except ModelUnavailable:
                    self.queue.append((kind_, item))
                    continue
                if text.strip():
                    self.handle_text(text, item["agent"], source="voice:queued")
            else:
                self.handle_text(item["text"], item["agent"], source="queued", skip_functions=set(item.get("skip", [])))

    # ------------------------------------------------------------------ keybinds
    def _keybind(self, action: str, params: dict[str, Any]) -> None:
        if action == "toggle":
            self.toggle()
        elif action == "abort":
            self.abort()
        elif not self.is_on():
            self._refuse_if_off()
        elif action == "text_request":
            self.publish("show_text_request", {})
        elif action == "voice_request":
            try:
                self.voice_request(params.get("agent"))
            except ValueError as exc:
                self.publish("notice", {"text": str(exc)})
        elif action == "review":
            self.publish("show_review", {})

    # ------------------------------------------------------------------ handoff / control planning
    def handoff(self, ctx: FunctionContext, to: str, message: str) -> str:
        target = self.agent_by_name(to)
        if target is None:
            raise FunctionError(f"there's no agent called {to}")
        if target == ctx.agent_id:
            raise FunctionError("an agent can't hand off to itself")
        allowed = ctx.agent.get("handoff_to") or []
        if "*" not in allowed and target not in allowed:
            raise FunctionError(f"{ctx.agent.get('name')} isn't allowed to talk to {self.agents()[target]['name']}")
        receiver = self.agents()[target]
        funcs = self.registry.enabled_for(receiver)
        listing = "\n".join(f"- {f.name}: {f.description}" for f in funcs)
        am = agent_memory(ctx.agent, self.settings)
        recent = self.recent_for(ctx.agent_id, ctx.agent, am["recent"], am["own_only"], ctx.entry["id"])
        convo = "\n".join(f"User: {r['text']}\n{self.agents().get(r['agent'] or '', {}).get('name', 'Agent')}: "
                          f"{r['result']}" for r in recent)
        composed = None
        try:
            composed = self.models.respond(
                ctx.agent,
                f"You are handing a request to {receiver['name']}, another assistant, whose abilities are:\n{listing}\n\n"
                f"Recent conversation:\n{convo or '(none)'}\n\nThe user asked: \"{message}\"\n\n"
                f"Write the message to send to {receiver['name']}, starting with what they should do and including "
                "only the relevant information. Reply with the message only.", ctx=None, raw=True)
        except ModelUnavailable:
            composed = None
        text = composed or (f"{message}\n\nContext:\n{convo}" if convo else message)
        ctx.trace("handoff", to=target, message=text)
        ctx.state("responding", f"Handing off to {receiver['name']}")
        # the receiving agent can't hand off again (no ping-pong between agents)
        res = self.handle_text(text, target, source=f"handoff:{ctx.agent_id}", wait=True,
                               skip_functions={"handoff"})
        entry = self.history.get(res.get("id", "")) or {}
        return entry.get("response", "")

    def plan_control(self, ctx: FunctionContext, instruction: str) -> list[dict[str, Any]]:
        from ..functions.builtins import CONTROL_ACTIONS_DOC
        info = []
        try:
            x, y = dk.mouse_position()
            info.append(f"Mouse is at x={x}, y={y}.")
        except dk.DesktopUnavailable:
            pass
        try:
            f = dk.focused()
            if f:
                info.append(f"Focused app: {f.app} ({f.title}) at x={f.x} y={f.y} size {f.w}x{f.h}.")
        except dk.DesktopUnavailable:
            pass
        targets: list[tuple[str, int, int]] = []
        box = None
        # the screen's text for anything that isn't plain keys / typing (it used to be only for "click",
        # "press", "button", "select", "open" -- "tap the", "hit the", "go to" got no screen at all, and the
        # model made coordinates up)
        keys_only = re.match(r"^\s*(?:type|press|hold|release|let go|scroll)\b", instruction, re.I) and \
            not re.search(r"\b(click|button|icon|link|on (?:the|my) screen)\b", instruction, re.I)
        if shutil.which("tesseract") and not keys_only:
            try:
                from ..functions.partials.screen import Mapper, _group_lines, _image_size, ocr_words, screenshot
                shot = screenshot()
                try:
                    mp = Mapper(_image_size(shot))
                    lines = _group_lines(ocr_words(shot))[:120]
                finally:
                    shot.unlink(missing_ok=True)
                bx, by, bw, bh = box = mp.box
                info.append(f"The desktop spans x={bx}..{bx + bw - 1}, y={by}..{by + bh - 1}. To point at "
                            "something, use an absolute move to its coordinates below "
                            '({"do":"move","x":X,"y":Y,"absolute":true}).')
                for l in lines:
                    px, py = mp.point(l["x"] + l["w"] / 2, l["y"] + l["h"] / 2)
                    targets.append((l["text"], int(px), int(py)))
                info.append("Text on screen (centre x,y): " + "; ".join(f"'{t}'@{x},{y}" for t, x, y in targets))
            except Exception as exc:
                ctx.think(f"Couldn't read the screen: {exc}")
        prompt = (f"Plan keyboard/mouse actions for: {instruction}\n\n" + "\n".join(info) +
                  f"\n\nActions format: {CONTROL_ACTIONS_DOC}\nReply with the JSON list only.")
        raw = self.models.respond(ctx.agent, prompt, ctx=ctx, raw=True)
        if raw is None:
            raise FunctionError("planning control actions needs the local response model")
        m = re.search(r"\[.*\]", raw, re.S)
        try:
            actions = json.loads(m.group(0)) if m else None
        except ValueError:
            actions = None
        if not isinstance(actions, list):
            raise FunctionError("couldn't plan those actions")
        actions = self._tidy_plan(actions, targets, box, ctx)
        ctx.trace("planned", actions=actions)
        return actions

    @staticmethod
    def _tidy_plan(actions: list[Any], targets: list[tuple[str, int, int]], box: Any, ctx: Any = None) -> list[Any]:
        """Small models get the coordinates nearly right and the rest wrong: a move to a screen position
        given without "absolute" (so it was taken as relative -- the mouse flew off somewhere), or a few
        pixels beside the text it meant. Moves that are clearly screen positions become absolute, and
        land on the nearest text on screen when one is close."""
        out = []
        for a in actions:
            if isinstance(a, dict) and a.get("do") == "move" and box is not None:
                a = dict(a)
                x, y = float(a.get("x", 0)), float(a.get("y", 0))
                bx, by, bw, bh = box
                inside = bx <= x < bx + bw and by <= y < by + bh
                if "absolute" not in a and inside and (x > 40 or y > 40):
                    a["absolute"] = True
                if a.get("absolute") and targets:
                    t, tx, ty = min(targets, key=lambda t: (t[1] - x) ** 2 + (t[2] - y) ** 2)
                    if (tx - x) ** 2 + (ty - y) ** 2 <= 90 ** 2 and (tx, ty) != (x, y):
                        if ctx is not None:
                            ctx.think(f"Moved the planned point onto '{t}' ({tx},{ty}; the plan said {x:.0f},{y:.0f})")
                        a["x"], a["y"] = tx, ty
            out.append(a)
        return out

    # ------------------------------------------------------------------ overlay process
    def _overlay_argv(self, popups: bool) -> list[str]:
        """``jeeves overlay [--popups]`` through the real command: inside the Nix
        wrapper a bare ``python -m`` child wouldn't find PySide6 or the Qt plugins."""
        extra = ["--popups"] if popups else []
        exe = os.environ.get("JEEVES_BIN") or shutil.which("jeeves")
        if exe:
            return [exe, "overlay", *extra]
        return [sys.executable, "-m", "jeeves.overlay", *extra]

    @staticmethod
    def _overlay_env(env: dict[str, str]) -> dict[str, str]:
        """The child finds this same jeeves package: started as `python -m jeeves.overlay` from another
        folder, it couldn't ("No module named 'jeeves'") and died, over and over."""
        root = str(Path(__file__).resolve().parent.parent.parent)
        env = dict(env)
        env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        return env

    def _ensure_overlay(self) -> None:
        """Keep both overlay processes running while there's a display: the indicator
        process (layer-shell) and the popups process (Review, Text Request, imports)."""
        if os.environ.get("JEEVES_NO_OVERLAY"):
            return
        env = graphical_env()
        if not (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            return
        now = time.time()
        for name, popups in (("indicators", False), ("popups", True)):
            proc = self._overlays.get(name)
            if proc is not None and proc.poll() is None:
                continue
            fails, retry_at = self._overlay_fails.get(name, (0, 0.0))
            logfile = paths.state_dir() / f"overlay-{name}.log"
            if proc is not None:
                lived = now - getattr(proc, "started_at", now)
                if proc.returncode == 3:                    # another overlay already runs (e.g. an older one)
                    if fails == 0:
                        log.info("overlay (%s): another copy is already running; using that one", name)
                    self._overlays.pop(name, None)
                    self._overlay_fails[name] = (1, now + 60)
                    continue
                if proc.returncode != 0 or lived < 10:      # crashed, or quit straight away (even "cleanly")
                    fails += 1
                    # keeps failing: back off to every 10 minutes and say so once -- restarting a process that
                    # dies at once every few seconds kept the CPU busy (and warm) with nothing on screen
                    retry_at = now + (min(60, 2 ** fails) if fails < 6 else 600)
                    if fails == 6:
                        self.publish("notice", {"text": f"The on-screen {name} keep failing to start; trying again "
                                                        "every 10 minutes. See `journalctl --user -u jeeves`."})
                    try:
                        tail = logfile.read_text(errors="replace").strip().splitlines()[-6:]
                    except OSError:
                        tail = []
                    log.warning("overlay (%s) exited with %s after %.0fs; restarting in %ds. Last output:\n  %s\n"
                                "  (full output: %s; command: %s)", name, proc.returncode, lived,
                                min(60, 2 ** fails), "\n  ".join(tail) or "(nothing)", logfile,
                                " ".join(self._overlay_argv(popups)))
                else:
                    fails = 0
                self._overlays.pop(name, None)
                self._overlay_fails[name] = (fails, retry_at)
            if now < retry_at:
                continue
            try:
                logfile.parent.mkdir(parents=True, exist_ok=True)
                out = open(logfile, "wb")
                proc = subprocess.Popen(self._overlay_argv(popups), env=self._overlay_env(env), stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=subprocess.STDOUT)
                out.close()
                proc.started_at = now                       # type: ignore[attr-defined]
                self._overlays[name] = proc
            except OSError as exc:
                log.warning("couldn't start the overlay (%s): %s", name, exc)
                self._overlay_fails[name] = (fails + 1, now + 30)

    # ------------------------------------------------------------------ status
    def overlay_status(self) -> dict[str, str]:
        out = {}
        for name in ("indicators", "popups"):
            proc = self._overlays.get(name)
            fails, retry_at = self._overlay_fails.get(name, (0, 0.0))
            if proc is not None and proc.poll() is None:
                out[name] = "running"
            elif fails:
                out[name] = f"not running (failed {fails}x; see {paths.state_dir() / f'overlay-{name}.log'})"
            else:
                out[name] = "not started (no display?)" if proc is None else f"exited {proc.returncode}"
        return out

    def status(self) -> dict[str, Any]:
        from .. import __build__
        return {
            "build": __build__,
            "enabled": self.is_on(),
            "agents": {aid: {"name": a.get("name"), "active": self.agent_active(a)} for aid, a in self.agents().items()},
            "listeners": {src: lst.status for src, lst in self.listeners.items()},
            "wake": dict(self._why_modes),
            "overlays": self.overlay_status(),
            "keyboard": self.keyboard.status if self.keyboard else "off",
            "hold_to_listen": self.hold_keys(),
            "indicators": list(self.indicators.values()),
            "extended": list(self.extended),
            "queue": len(self.queue),
            "control_available": self.control.available(),
            "puppetry": self.puppetry.installed(),
            "desktop": desktop_name(),
            "problems": self.registry.problems,
        }
