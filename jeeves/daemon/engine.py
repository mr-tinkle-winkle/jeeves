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
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .. import config, paths
from ..functions.base import Cancelled, FunctionError
from ..functions.registry import Registry
from ..models.manager import ModelManager, ModelUnavailable
from ..util import desktop as desktop_name
from ..util import graphical_env, normalize, similarity
from . import desktop as dk
from .audio import Playback, output_targets
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
        self.training = Training(self.settings, self.registry)
        self.intent.extra_examples = self.training.intent_examples
        self.control = Control(self.settings)
        self.on_exit: Callable[[], None] | None = None     # set by the server: stop the daemon
        self.interrupted: dict[str, list[str]] = {}       # agent -> requests paused by calling its name
        self.puppetry = Puppetry(self.settings)
        self.online = Online(self.settings)
        self.wikipedia = Wikipedia(self.settings, self.publish)
        self.timers = Timers(self._timer_fired, self.publish)
        self.triggers = Triggers(self.settings, self._trigger_fired)
        self.imports = Imports(self.registry, self.publish)
        from .jumpin import JumpIn
        self.jump_in = JumpIn(self)
        self.keyboard: Keyboard | None = Keyboard(self.settings, self._keybind) if start_io else None
        self._lock = threading.RLock()
        self._apply_lock = threading.Lock()
        self.speaking = 0
        self.speaking_until = 0.0
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
        self.models.preload()
        self.triggers.start()
        if self.keyboard:
            self.keyboard.start()
        self.apply_settings()
        threading.Thread(target=self._housekeeping, daemon=True, name="jeeves-housekeeping").start()

    def stop(self) -> None:
        self._stop.set()
        self.abort()
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

    def _apply_settings_locked(self) -> None:
        self._apply_power()
        if self.is_on():
            # "Jeeves-Microphone" always exists while Jeeves is on, so Discord/OBS can list it before an
            # agent first speaks into it (it carries your own mic in the meantime)
            from .audio import ensure_virtual_mic
            ensure_virtual_mic(self.settings.get("audio.virtual_mic_sink", "jeeves-mic"),
                               bool(self.settings.get("audio.virtual_mic_include_mic", True)),
                               self.settings.get("audio.microphone", ""))
        self.models.prune()
        wanted = set()
        if self._needs_source("microphone"):
            wanted.add("microphone")
        if self._needs_source("desktop"):
            wanted.add("desktop")
        if self.is_on():
            for a in self.active_agents().values():          # agents listening to one exact device
                if a.get("listen_to") == "device" and a.get("listen_device"):
                    src = f"device:{a['listen_device']}"
                    if self.call_names(src) or self.jump_in.wants(src):
                        wanted.add(src)
        for src in list(self.listeners):
            if src not in wanted:
                self.listeners.pop(src).stop()
        for src in wanted:
            if src not in self.listeners or not self.listeners[src].is_alive():
                lst = Listener(self, src)
                self.listeners[src] = lst
                lst.start()
        self.publish("status", self.status())

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
            return bool(self.settings.get("wake_word.enabled", True) and self.call_names("microphone"))
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
            except Exception:
                log.exception("housekeeping failed")

    # ------------------------------------------------------------------ events
    def publish(self, topic: str, data: Any) -> None:
        try:
            self._publish_fn(topic, data)
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

    def _listens(self, agent: dict[str, Any], source: str) -> bool:
        lt = agent.get("listen_to", "user")
        if lt == "device":
            return bool(agent.get("listen_device")) and source == f"device:{agent['listen_device']}"
        return lt == "both" or (lt == "user" and source == "microphone") or (lt == "desktop" and source == "desktop")

    def call_names(self, source: str) -> list[str]:
        names = []
        for a in self.active_agents().values():
            if self._listens(a, source):
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

    # ------------------------------------------------------------------ listening
    def detection_modes(self, source: str) -> set[str]:
        modes: set[str] = set()
        summary = self.summary.enabled() and source in (self.settings.get("summary.sources") or [])
        keyword_triggers = any(t.get("event") == "audio_keyword" and t.get("source", "microphone") in (source, "both")
                               for t in self.triggers.all())
        names = self.call_names(source)
        wake_on = self.settings.get("wake_word.enabled", True) and not self.summary.enabled()
        if names and wake_on:
            engine = self.settings.get("wake_word.engine", "vosk")
            if engine == "vosk" and self.models.wake_spotter() is not None:
                modes.add("vosk")
            else:
                modes.add("transcribe")
        if summary or keyword_triggers or (names and self.summary.enabled()) or self.jump_in.wants(source):
            modes.add("transcribe")
        return modes

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
        stage = "listening"
        self.set_indicator(s.request_id, agent_id, stage, "extended prompt mode" if mode == "extended" else "",
                           source=source, mode=mode)
        return s

    def end_session(self, s: Session) -> None:
        with self._lock:
            if self.sessions.get(s.source) is s:
                del self.sessions[s.source]
        s.ended = True
        self.run_async(self.apply_settings)      # the mic may not be needed any more
        if not s.got_speech:
            self.set_indicator(s.request_id, s.agent_id, "idle")
            if s.interrupting:                   # called by name, then nothing said: carry on
                self._resume(s.interrupting)
            return
        self.run_async(self._finish_session, s)

    def _finish_session(self, s: Session) -> None:
        pcm = s.pcm()
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
        self.set_indicator(s.request_id, s.agent_id, "transcript", text)
        if s.mode == "answer":
            self.set_indicator(s.request_id, s.agent_id, "idle")
            self._deliver_answer(s.agent_id, text)
            return
        if s.interrupting:
            if self._after_interruption(s.interrupting, text):
                self.set_indicator(s.request_id, s.agent_id, "idle")
                return
        if not text.strip():
            self.set_indicator(s.request_id, s.agent_id, "idle")
            return
        self.handle_text(text, s.agent_id, source=f"voice:{s.source}", request_id=s.request_id)

    def transcribe(self, pcm: bytes, agent: dict[str, Any] | None = None) -> str:
        stt = self.models.stt(agent)
        lang = self.settings.get("models.stt.language", "en")
        return stt.transcribe(pcm, prompt=self.training.initial_prompt(), language=lang)

    def on_wake(self, source: str, name: str, conf: float, after: list[bytes]) -> None:
        if self.sessions.get(source) is not None or not self.is_on():
            return
        aid = self.agent_by_name(name, source)
        if aid is None:
            return
        agent = self.agents()[aid]
        if conf < self.threshold(agent):
            log.debug("wake '%s' below threshold (%.2f)", name, conf)
            return
        if self._hears_itself(aid, name):
            return
        paused = self._interrupt(aid)
        if "stt" in self.models.suspended:
            self.flash_unavailable(aid, f"speech recognition is unloaded while {self.models.suspended['stt']} is open")
        s = self.open_session(source, aid, "extended" if aid in self.extended else "request")
        s.interrupting = paused
        if after:
            s.frames = list(after)
            s.got_speech = True
            s.last_voice = time.time()

    def on_utterance(self, source: str, pcm: bytes) -> None:
        """Transcribe-everything path: Summary log, audio keyword triggers, and
        call names found in the transcript."""
        try:
            text = self.transcribe(pcm)
        except ModelUnavailable:
            return
        if not text.strip():
            return
        self.summary.add(source, text)
        self.triggers.on_transcript(text, source)
        self.jump_in.heard(source, text)
        if self.sessions.get(source) is not None or self.answer_pending(source):
            return
        wake_by_text = (self.summary.enabled() or self.settings.get("wake_word.engine") == "stt-match"
                        or self.models.wake_spotter() is None) and self.settings.get("wake_word.enabled", True) \
            or self.summary.enabled()
        aid, rest, _ = self.split_agent(text, source)
        if aid is None:
            self.jump_in.consider(source)      # nobody was addressed: maybe an agent wants to chime in
            return
        if not wake_by_text:                   # the wake word model handles addressed requests
            return
        if self._hears_itself(aid, text):
            return
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
                    if c.agent_id == agent_id and not c.dry_run and not c.suspend_event.is_set()]

    def _hears_itself(self, agent_id: str, heard: str) -> bool:
        """The mic picking up the agent saying its own name isn't the user calling it."""
        h = normalize(heard)
        with self._lock:
            ctxs = [c for c in self.active.values() if c.agent_id == agent_id]
        return any(h and h.split()[0] in normalize(getattr(c, "saying", "") or "") for c in ctxs)

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
        if agent_id is None:
            self.set_indicator(s.request_id, None, "listening", "say an agent's name first",
                               source="microphone", mode="request")
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
            entry = self.history.get(request_id)
            return {"thoughts": [], "looking_at": "", "trace": entry["trace"] if entry else []}
        return {"thoughts": ctx.thoughts, "looking_at": ctx.looking_at, "trace": ctx.entry["trace"]}

    # ------------------------------------------------------------------ requests
    def handle_text(self, text: str, agent_id: str | None = None, source: str = "text",
                    request_id: str | None = None, skip_functions: set[str] | None = None,
                    dry_run: bool = False, wait: bool = False) -> dict[str, Any]:
        text = text.strip()
        if not dry_run and self._refuse_if_off():
            return {"error": "Jeeves is turned off"}
        if agent_id is None:
            heard_on = source.split(":", 1)[1] if source.startswith("voice:") else None
            agent_id, rest, _ = self.split_agent(text, heard_on)
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
        entry = new_entry(text, agent_id, source)
        if request_id:
            entry["id"] = request_id
        entry["dry_run"] = dry_run
        self.history.add(entry)
        ctx = FunctionContext(self, agent_id, agent, entry, dry_run=dry_run)
        if not dry_run:
            with self._lock:
                self.active[entry["id"]] = ctx
        if wait or dry_run:
            self._run(ctx, skip_functions or set())
        else:
            self.run_async(self._run, ctx, skip_functions or set())
        return {"id": entry["id"], "agent": agent_id, "entry": entry if (wait or dry_run) else None}

    def _decide(self, ctx: FunctionContext, text: str, skip: set[str]) -> Decision:
        agent = dict(ctx.agent)
        if skip:
            agent["functions"] = dict(agent.get("functions") or {}, **{s: False for s in skip})
        return self.intent.decide(agent, text, ctx)

    def _run(self, ctx: FunctionContext, skip: set[str]) -> None:
        entry = ctx.entry
        text = entry["text"]
        try:
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
            if decision.refusal:
                entry["function"], entry["status"] = None, "refused"
                entry["response"] = decision.refusal
                if not ctx.dry_run:
                    ctx.say(decision.refusal)
                return
            threshold = float(self.settings.get("general.unclear_confidence", 0.45))
            rounds = 0
            while (decision.function is None or decision.confidence < threshold) and rounds < 2:
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
        f = self.registry.get(decision.function)
        ctx.trace("would_call", function=decision.function, args=decision.args)
        if f is None:
            return
        if f.steps is not None or f.kind == "full":
            try:
                result = ctx.call(decision.function, **decision.args)
                ctx.trace("dry_result", result=str(result)[:500])
            except FunctionError as exc:
                ctx.trace("error", message=str(exc))

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
        self.jump_in.spoke(ctx.agent_id, text)       # part of the conversation even if TTS fails
        try:
            pcm, rate = self._synth(agent, text)
        except Exception as exc:   # speech failing must never lose the answer
            ctx.trace("tts_failed", reason=str(exc))
            self._tts_problem(f"Text to speech failed: {exc}")
            return
        targets = output_targets(agent.get("output_to", "speakers"), self.settings.get("audio.speaker", ""),
                                 self.settings.get("audio.virtual_mic_sink", "jeeves-mic"),
                                 bool(self.settings.get("audio.virtual_mic_include_mic", True)),
                                 self.settings.get("audio.microphone", ""))
        pb = Playback(pcm, rate, targets)
        ctx.playback = pb
        ctx.saying = text
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
        if pb.error:
            ctx.trace("playback_failed", reason=pb.error)
            self._tts_problem(f"Couldn't play speech: {pb.error}")
        ctx.check_cancelled()

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
        """Agents page > Preview: say a sample in this (possibly unsaved) agent's voice, on the speakers."""
        name = agent.get("name") or "your assistant"
        pcm, rate = self._synth(agent, text or f"Hello, I'm {name}. This is how I sound.")
        pb = Playback(pcm, rate, [self.settings.get("audio.speaker", "")])
        with self._lock:
            self.speaking += 1
        try:
            pb.play()
        finally:
            with self._lock:
                self.speaking -= 1
                self.speaking_until = time.time() + 0.5
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
        if shutil.which("tesseract") and re.search(r"\b(click|press|button|select|open)\b", instruction, re.I):
            try:
                from ..functions.partials.screen import Mapper, _group_lines, _image_size, ocr_words, screenshot
                shot = screenshot()
                try:
                    mp = Mapper(_image_size(shot))
                    lines = _group_lines(ocr_words(shot))[:120]
                finally:
                    shot.unlink(missing_ok=True)
                bx, by, bw, bh = mp.box
                info.append(f"The desktop spans x={bx}..{bx + bw - 1}, y={by}..{by + bh - 1} "
                            "(use absolute moves with these coordinates).")
                info.append("Text on screen (centre x,y): " + "; ".join(
                    "'{}'@{},{}".format(l["text"], *mp.point(l["x"] + l["w"] / 2, l["y"] + l["h"] / 2))
                    for l in lines))
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
        ctx.trace("planned", actions=actions)
        return actions

    # ------------------------------------------------------------------ overlay process
    def _overlay_argv(self, popups: bool) -> list[str]:
        """``jeeves overlay [--popups]`` through the real command: inside the Nix
        wrapper a bare ``python -m`` child wouldn't find PySide6 or the Qt plugins."""
        extra = ["--popups"] if popups else []
        exe = os.environ.get("JEEVES_BIN") or shutil.which("jeeves")
        if exe:
            return [exe, "overlay", *extra]
        return [sys.executable, "-m", "jeeves.overlay", *extra]

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
            if proc is not None:
                if proc.returncode != 0:
                    fails += 1
                    log.warning("overlay (%s) exited with %s; restarting in %ds", name, proc.returncode,
                                min(60, 2 ** fails))
                    retry_at = now + min(60, 2 ** fails)
                self._overlays.pop(name, None)
                self._overlay_fails[name] = (fails, retry_at)
            if now < retry_at:
                continue
            try:
                self._overlays[name] = subprocess.Popen(self._overlay_argv(popups), env=env, stdin=subprocess.DEVNULL)
            except OSError as exc:
                log.warning("couldn't start the overlay (%s): %s", name, exc)
                self._overlay_fails[name] = (fails + 1, now + 30)

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.is_on(),
            "agents": {aid: {"name": a.get("name"), "active": self.agent_active(a)} for aid, a in self.agents().items()},
            "listeners": {src: lst.status for src, lst in self.listeners.items()},
            "keyboard": self.keyboard.status if self.keyboard else "off",
            "indicators": list(self.indicators.values()),
            "extended": list(self.extended),
            "queue": len(self.queue),
            "control_available": self.control.available(),
            "puppetry": self.puppetry.installed(),
            "desktop": desktop_name(),
            "problems": self.registry.problems,
        }
