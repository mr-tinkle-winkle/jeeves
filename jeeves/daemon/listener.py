"""Listening: one thread per audio source (microphone, desktop audio).

Per 30 ms frame:
  1. an open listening *session* (a request, an answer, extended prompt mode)
     takes the audio until the user stops talking;
  2. otherwise, while an agent is waiting for an answer, speech opens an
     answer session (no wake word needed);
  3. otherwise the wake word model listens for agent call names. A Vosk
     grammar spotter reacts per phrase; the STT-match / Summary path
     transcribes every utterance and looks for names in the text.

A rolling buffer of the last 20 s lets "Jeeves, open all my apps" said in one
breath work: when the name is recognised at the end of the phrase, the audio
after the name is already in the buffer.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .audio import FRAME_MS, Capture, rms

log = logging.getLogger("jeeves.listener")

RING_FRAMES = 20_000 // FRAME_MS
PREROLL_FRAMES = 10
START_FRAMES = 3                 # consecutive voiced frames that count as speech
NO_SPEECH_TIMEOUT = 6.0
CONFIG_REFRESH = 0.5            # s between re-reading what a source listens for


@dataclass
class Session:
    source: str
    agent_id: str | None
    mode: str                     # request | answer | extended | training
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    frames: list[bytes] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    last_voice: float = 0.0
    got_speech: bool = False
    extend_until: float = 0.0
    hold: bool = False
    max_seconds: float = 60.0
    ended: bool = False
    text: str = ""                # training: the phrase being read
    voiced_frames: int = 0        # how much of it was actually speech
    suspended: bool = False       # right-click > Suspend: stop taking audio until resumed
    interrupting: list[str] = field(default_factory=list)   # requests paused because their agent was called
    retried: bool = False         # reopened because only the name was heard the first time

    def feed(self, frame: bytes, voiced: bool, now: float) -> None:
        self.frames.append(frame)
        if voiced:
            self.last_voice = now
            self.got_speech = True
            self.voiced_frames += 1

    def pcm(self) -> bytes:
        return b"".join(self.frames)

    def should_end(self, now: float, eos: float) -> bool:
        if self.ended:
            return True
        if self.hold or now < self.extend_until:
            return False
        if now - self.started >= self.max_seconds:
            return True
        if self.got_speech:
            return now - self.last_voice >= eos
        return now - self.started >= NO_SPEECH_TIMEOUT


class Segmenter:
    """Splits continuous audio into utterances by voice activity."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.active = False
        self.streak = 0
        self.last_voice = 0.0
        self.pre: deque[bytes] = deque(maxlen=PREROLL_FRAMES)

    def feed(self, frame: bytes, voiced: bool, now: float, eos: float) -> bytes | None:
        if not self.active:
            self.pre.append(frame)
            self.streak = self.streak + 1 if voiced else 0
            if self.streak >= START_FRAMES:
                self.active = True
                self.frames = list(self.pre)
                self.last_voice = now
            return None
        self.frames.append(frame)
        if voiced:
            self.last_voice = now
        if now - self.last_voice >= eos or len(self.frames) * FRAME_MS > 30_000:
            out = b"".join(self.frames)
            self.frames, self.active, self.streak = [], False, 0
            self.pre.clear()
            return out
        return None


def speech_threshold(floor: float, configured: float) -> float:
    """What counts as speech over this background: on a quiet source 3x the background (at least
    0.003, at most the configured level); over a loud steady background (music, a game) just enough
    above it to stand out."""
    if floor * 3 <= configured:
        return max(0.003, floor * 3)
    return max(configured, floor * 1.6)


class Listener(threading.Thread):
    def __init__(self, engine: Any, source: str) -> None:
        super().__init__(daemon=True, name=f"jeeves-listen-{source}")
        self.engine = engine
        self.source = source
        self.ring: deque[bytes] = deque(maxlen=RING_FRAMES)
        self.frame_no = 0
        self.spot_base = 0           # frame_no when the wake spotter's stream started
        self.spotter_names: list[str] = []
        self.wake_rec: Any = None          # this listener's own recognizer (never shared)
        self.wake_owner: Any = None
        self.segmenter = Segmenter()
        self.answer_streak = 0
        self.capture: Capture | None = None
        self.floor = 0.002            # this source's background level (from the last few seconds of audio)
        self.levels: deque[float] = deque(maxlen=170)      # ~5 s of frame levels
        self.trailing = 0             # words the wake model heard after the name
        self._stop = threading.Event()
        self.status = "starting"
        self._cfg: tuple[set[str], list[str], float, Any] | None = None
        self._cfg_at = 0.0
        self._vad: float | None = None

    def stop(self) -> None:
        self._stop.set()
        if self.capture is not None:
            self.capture.close()

    def run(self) -> None:
        while not self._stop.is_set():
            if self.source.startswith("device:"):
                dev, kind = self.source[7:], "device"
            else:
                setting = "audio.microphone" if self.source == "microphone" else "audio.desktop"
                dev, kind = self.engine.settings.get(setting, ""), self.source
            try:
                self.capture = Capture(dev, kind)
            except RuntimeError as exc:
                self.status = str(exc)
                log.warning("%s capture unavailable: %s", self.source, exc)
                self._stop.wait(10)
                continue
            self.status = "listening"
            try:
                for frame in self.capture.frames():
                    if self._stop.is_set():
                        break
                    try:
                        self.process(frame)
                    except Exception:
                        log.exception("listener frame handling failed")
            finally:
                self.capture.close()
            if not self._stop.is_set():
                self.status = "capture stopped; restarting"
                self._stop.wait(2)

    # ------------------------------------------------------------------
    def process(self, frame: bytes) -> None:
        eng = self.engine
        now = time.time()
        if self.source != "microphone" and (eng.speaking or now < eng.speaking_until):
            frame = b"\0" * len(frame)      # Jeeves' own voice is on the desktop audio: don't hear it
        level = rms(frame)
        self.levels.append(level)
        if self.frame_no % 15 == 0 and len(self.levels) >= 30:
            # background = a low percentile of the last ~5 s, every frame counted -- so a steady sound
            # (fan, music, game) can never be mistaken for endless speech
            self.floor = sorted(self.levels)[len(self.levels) // 5]
        voiced = level > self.threshold()
        self.ring.append(frame)
        self.frame_no += 1
        modes, names, eos, spotter = self._config(now)
        hits: list[tuple[str, float]] = []
        if spotter is not None:
            if self.wake_rec is None or names != self.spotter_names or self.wake_owner is not spotter:
                try:
                    self.wake_rec = spotter.recognizer(names)
                except Exception:
                    log.exception("wake word model failed to start")
                    self.wake_rec = None
                self.wake_owner = spotter
                self.spotter_names = names
                self.spot_base = self.frame_no - 1
            try:
                hits = self.wake_rec.feed(frame) if self.wake_rec is not None else []
            except Exception:
                log.exception("wake word model failed")
                hits = []
        else:
            self.wake_rec = None

        if "transcribe" in modes:
            utterance = self.segmenter.feed(frame, voiced, now, eos)
            if utterance is not None:
                eng.run_async(eng.on_utterance, self.source, utterance)

        session = eng.sessions.get(self.source)
        if session is not None and session.suspended:
            return
        if session is not None:
            session.feed(frame, voiced, now)
            if session.mode == "extended":
                chunk = eng.extended_chunk_ready(session, now, eos)
                if chunk is not None:
                    eng.run_async(eng.on_extended_chunk, session, chunk)
            elif session.should_end(now, eos):
                eng.end_session(session)
            return

        if eng.answer_pending(self.source):
            self.answer_streak = self.answer_streak + 1 if voiced else 0
            if self.answer_streak >= START_FRAMES:
                self.answer_streak = 0
                agent_id = eng.answer_pending(self.source)
                s = eng.open_session(self.source, agent_id, "answer")
                s.frames = list(self.ring)[-PREROLL_FRAMES:]
                s.got_speech, s.last_voice = True, now
                s.voiced_frames = START_FRAMES
            return

        if hits:
            best = max(hits, key=lambda h: h[1])
            after = self._audio_after_name(self.wake_rec, best[0])
            eng.on_wake(self.source, best[0], best[1], after, threshold=self.threshold(), words=self.trailing)

    def _config(self, now: float) -> tuple[set[str], list[str], float, Any]:
        """What this source listens for, refreshed twice a second rather than every 30 ms frame:
        working it out deep-copies settings and agents and (for app rules) scans the window and
        process lists, which is too much to do ~33 times a second per source on the audio thread."""
        if self._cfg is None or now - self._cfg_at >= CONFIG_REFRESH:
            eng = self.engine
            modes = eng.detection_modes(self.source)
            spotter = eng.models.wake_spotter() if "vosk" in modes else None
            names = eng.call_names(self.source) if spotter is not None else []
            eos = float(eng.settings.get("general.end_of_speech_seconds", 1.2))
            self._cfg, self._cfg_at = (modes, names, eos, spotter), now
            self._vad = float(eng.settings.get("audio.vad_threshold", 0.012))
        return self._cfg

    def threshold(self) -> float:
        if self._vad is not None:
            return speech_threshold(self.floor, self._vad)
        return speech_threshold(self.floor, float(self.engine.settings.get("audio.vad_threshold", 0.012)))

    def _audio_after_name(self, spotter: Any, name: str) -> list[bytes]:
        """Frames recorded after the call name in the phrase just recognised. Kept even when the wake model heard no words after the name: the name is now
        reported mid-phrase, and the start of the request may already be in them. Whether they hold
        speech is decided separately (Engine._speech_in)."""
        words = getattr(spotter, "last_result", []) or []
        self.trailing = 0
        end_s = None
        parts = name.split()
        for i in range(len(words) - len(parts) + 1):
            if [w.get("word") for w in words[i:i + len(parts)]] == parts:
                end_s = float(words[i + len(parts) - 1].get("end", 0))
                self.trailing = len([w for w in words[i + len(parts):] if w.get("word") != name])
        if end_s is None:
            return []
        start_frame = self.spot_base + int(end_s * 1000 / FRAME_MS)
        back = self.frame_no - start_frame
        if back <= 0 or back > len(self.ring):
            return []
        return list(self.ring)[-back:]
