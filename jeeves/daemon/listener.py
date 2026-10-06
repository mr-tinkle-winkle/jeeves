"""Listening: one thread per audio source (microphone, desktop audio).

Per 30 ms frame:
  0. the speech detector (Silero VAD; the level meter without it) says whether
     someone is speaking;
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
from .vad import SpeechDetector

log = logging.getLogger("jeeves.listener")

RING_FRAMES = 20_000 // FRAME_MS
PREROLL_FRAMES = 10
START_FRAMES = 3                 # consecutive voiced frames that count as speech
NO_SPEECH_TIMEOUT = 6.0
CONFIG_REFRESH = 0.5            # s between re-reading what a source listens for
HOLD_LIMIT = 300.0              # a held mic (or a stuck hold) still ends after this long
PHRASE_GRACE = 1.5              # extra wait while the wake model is still decoding a phrase
NAME_PREROLL = 0.25             # s of audio kept before the name, so speech recognition hears it too
TRUST_GAP = 0.6                 # s of quiet after the name that make it a call, even if misspelled later
BLIND_FRAMES = 10               # under this many level-meter speech frames, trust the wake model instead
SPEECH_RUN = 3                  # frames in a row that count as speech: a key press or a mouse click is one or
                                # two loud frames, and the speech detector can blip on a beat in music
LEVEL_RUN = SPEECH_RUN
MIN_SPEECH_LEVEL = 0.0008       # speech detector: quieter than this is digital silence, whatever it says
BACKUP_MAX_SECONDS = 10.0       # longer talk isn't checked for a missed call (Listener._backup)


@dataclass
class Session:
    source: str
    agent_id: str | None
    mode: str                     # request | answer | extended | training
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    frames: list[bytes] = field(default_factory=list)
    started: float = field(default_factory=lambda: time.time())
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
    # opened by a wake word: the audio of the name itself (transcribed with the request, so a
    # false wake -- "geez", "believes" -- can be told apart), and whether the wake was clear enough
    # to trust even when speech recognition doesn't spell the name out
    preroll: list[bytes] | None = None
    wake_conf: float = 1.0
    replied: bool = False         # it said its wake-up reply, so you were waiting for it
    reply_at: float = 0.0         # say the wake-up reply at this time if you're still quiet (0: no reply)
    tail_until: float = 0.0       # speech going on when it opened is the name's own end until then
    deaf_until: float = 0.0       # the agent's own wake-up reply is playing: don't take that as the request
    first_voice: float | None = None
    detected_frames: int = 0      # frames the speech detector (or level meter) called speech
    heard: bool = False           # the wake model decoded speech in this listen

    def feed(self, frame: bytes, voiced: bool, now: float, heard_until: float | None = None,
             sure: bool = True) -> None:
        """heard_until (level meter only): when the wake model last decoded speech in this session --
        over loud music or a game the level meter misses speech that the model still hears.
        sure: more than the speech detector's word for it (louder than the background, or the wake
        model decoding words) -- which tells the request from the name's own end, below."""
        self.frames.append(frame)
        if self.tail_until:
            # the speech detector still hears the name for a moment after it (longer over music): that
            # isn't the request starting, unless it's clearly more speech ("Jeeves set a timer", fast)
            if not voiced or now >= self.tail_until or sure:
                self.tail_until = 0.0
            else:
                return
        if voiced:
            self.last_voice = now
            self.got_speech = True
            self.voiced_frames += 1
            self.detected_frames += 1
            if self.first_voice is None:
                self.first_voice = now
        if heard_until is not None and heard_until > max(self.last_voice, self.started + 0.1):
            self.voiced_frames += int((heard_until - max(self.last_voice, self.started)) * 1000 / FRAME_MS)
            self.last_voice = heard_until
            self.got_speech = self.heard = True
            if self.first_voice is None:
                self.first_voice = heard_until

    def trusted(self) -> bool:
        """A wake clear enough to take even if speech recognition doesn't spell the name out: a confident
        name followed by a real pause (or by its wake-up reply). A name inside running speech has to
        show up in the transcript ("geez, that was loud" isn't a call)."""
        gap = (self.first_voice - self.started) if self.first_voice is not None else 99.0
        return self.wake_conf >= 0.9 and (self.replied or gap >= TRUST_GAP)

    def pcm(self) -> bytes:
        return b"".join(self.frames)

    def should_end(self, now: float, eos: float, phrase_open: bool = False) -> bool:
        if self.ended:
            return True
        if now - self.started >= (max(self.max_seconds, HOLD_LIMIT) if self.hold else self.max_seconds):
            return True                      # even a held mic can't keep it open forever
        if self.hold or now < self.extend_until or now < self.deaf_until:
            return False
        if self.got_speech:
            quiet = now - self.last_voice
            if phrase_open and self.heard and self.detected_frames < BLIND_FRAMES and quiet < eos + PHRASE_GRACE:
                # the level meter can't pick your voice out of the background (loud music, a game) but the
                # wake model is still decoding words: give it time to catch up before ending
                return False
            return quiet >= eos
        return now - max(self.started, self.deaf_until) >= NO_SPEECH_TIMEOUT


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
        self.voiced_ring: deque[bool] = deque(maxlen=RING_FRAMES)   # speech or not, for each frame in ring
        self.detector: SpeechDetector | None = None
        self.detector_kind = ""       # what the detector was made for ("auto" | "level")
        self._loud = 0                # frames in a row that sounded like speech
        self._above = 0               # frames in a row louder than the background
        self.trailing = 0             # words the wake model heard after the name
        self._stop = threading.Event()
        self.status = "starting"
        self._cfg: tuple[set[str], list[str], float, Any] | None = None
        self._cfg_at = 0.0
        self._vad: float | None = None
        self._detector_setting = "auto"
        self._audio_t: float | None = None      # the audio clock (see _clock)
        self._backup_skip = False                # the wake model already heard a name in this utterance
        self._now = 0.0                          # ... of the frame being processed

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
                self._audio_t = None
                for frame in self.capture.frames():
                    if self._stop.is_set():
                        break
                    try:
                        self.process(frame, backlog=self.capture.backlog())
                    except Exception:
                        log.exception("listener frame handling failed")
            finally:
                self.capture.close()
            if not self._stop.is_set():
                self.status = "capture stopped; restarting"
                self._stop.wait(2)

    # ------------------------------------------------------------------
    def _clock(self, backlog: int | None) -> float:
        """When this frame was recorded. Counted along the audio rather than read off the clock: when
        the computer is busy (a game loading) the listener falls behind and then catches up on several
        seconds of audio at once -- by the wall clock that looked like a long silence, and a listen
        ended before the request that was still waiting in the queue."""
        wall = time.time()
        if backlog is None:
            return wall
        t = self._audio_t + FRAME_MS / 1000 if self._audio_t is not None else wall
        if self._audio_t is None or t > wall or backlog == 0 and wall - t > 0.25:
            t = wall                                 # (re)start, or caught up: back in step with the clock
        self._audio_t = t
        return t

    def process(self, frame: bytes, backlog: int | None = None) -> None:
        eng = self.engine
        now = self._now = self._clock(backlog)
        if self.source != "microphone" and (eng.speaking or now < eng.speaking_until):
            frame = b"\0" * len(frame)      # Jeeves' own voice is on the desktop audio: don't hear it
        level = rms(frame)
        self.levels.append(level)
        if self.frame_no % 15 == 0 and len(self.levels) >= 30:
            # background = a low percentile of the last ~5 s, every frame counted -- so a steady sound
            # (fan, music, game) can never be mistaken for endless speech
            self.floor = sorted(self.levels)[len(self.levels) // 5]
        modes, names, eos, spotter = self._config(now)
        voiced = self._voiced(frame, level)
        self.ring.append(frame)
        self.voiced_ring.append(voiced)
        self.frame_no += 1
        hits: list[tuple[str, float]] = []
        if spotter is not None and names:
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

        session = eng.sessions.get(self.source)
        if "transcribe" in modes:
            utterance = self.segmenter.feed(frame, voiced, now, eos)
            if utterance is not None:
                eng.run_async(eng.on_utterance, self.source, utterance)
        elif "backup" in modes:
            self._backup(frame, voiced, now, eos, bool(hits), session)

        if hits and (session is None or session.mode in ("request", "answer")):
            # a name always gets through -- also while an agent waits for an answer (before, a pending
            # question on this source made every agent deaf to its name until it timed out)
            best = max(hits, key=lambda h: h[1])
            w = self._wake_audio(self.wake_rec, best[0])
            eng.on_wake(self.source, best[0], best[1], w["after"], threshold=self.threshold(),
                        words=w["trailing"], name_audio=w["name"], after_voiced=w["after_voiced"])
            if eng.sessions.get(self.source) is not session:
                return                       # a new listen starts with the audio after the name
        if session is not None and session.suspended:
            return
        if session is not None:
            if now < session.deaf_until:
                return                       # its own "Yes?" is playing: not part of the request
            # the wake model's word decoding stands in for a level meter that can't hear you over loud
            # sound; the speech detector can, and needs no stand-in
            level_meter = self.detector is None
            decoding = bool(self.wake_rec is not None and self.wake_rec.phrase_open)
            phrase_open = level_meter and decoding
            session.feed(frame, voiced, now, self._heard_until(now) if level_meter else None,
                         sure=level_meter or decoding or self._above >= SPEECH_RUN)
            if session.reply_at and now >= session.reply_at:
                if session.got_speech or now > session.reply_at + 1.5:
                    session.reply_at = 0.0           # you're already talking (or it's too late to say it)
                elif not voiced and not session.tail_until:
                    session.reply_at = 0.0           # you said the name and wait: "Yes?"
                    eng.wake_reply(session)
            if session.mode == "extended":
                chunk = eng.extended_chunk_ready(session, now, eos)
                if chunk is not None:
                    eng.run_async(eng.on_extended_chunk, session, chunk)
            elif session.should_end(now, eos, phrase_open):
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

    def _backup(self, frame: bytes, voiced: bool, now: float, eos: float, hit: bool, session: Any) -> None:
        """Utterances the wake word model heard no name in go to speech recognition, which catches the
        calls the model misses (Engine.on_backup_utterance). Not while a listen is open, an agent waits
        for an answer or speaks (it would hear itself), and not for long talk (a conversation with
        someone else: a call starts with the name, a few seconds in at most)."""
        eng = self.engine
        busy = session is not None or eng.speaking or now < eng.speaking_until or eng.answer_pending(self.source)
        if hit:
            self._backup_skip = True                 # the wake model has this one
        if busy:
            self.segmenter = Segmenter()
            self._backup_skip = False
            return
        was_active = self.segmenter.active
        utterance = self.segmenter.feed(frame, voiced, now, eos)
        if not was_active and self.segmenter.active:
            self._backup_skip = hit
        if utterance is None:
            return
        skip, self._backup_skip = self._backup_skip, False
        seconds = len(utterance) / (2 * 16000)
        if not skip and 0.4 <= seconds <= BACKUP_MAX_SECONDS:
            eng.run_async(eng.on_backup_utterance, self.source, utterance, self.frame_no)

    def _voiced(self, frame: bytes, level: float) -> bool:
        """Is someone speaking in this frame: the speech detector, or the level meter without it."""
        want = self._detector_setting
        if want != self.detector_kind:
            self.detector_kind = want
            self.detector = SpeechDetector.create() if want != "level" else None
        speech = False
        if self.detector is not None:
            try:
                speech = self.detector.feed(frame) and level >= MIN_SPEECH_LEVEL
            except Exception:  # noqa: BLE001
                log.exception("the speech detector failed; using the level meter")
                self.detector = None
        above = level > self.threshold()
        self._above = self._above + 1 if above else 0
        if self.detector is None:
            speech = above
        self._loud = self._loud + 1 if speech else 0
        return self._loud >= SPEECH_RUN

    def _wall(self, stream_s: float) -> float:
        """The time of a moment in the wake model's stream (its word times)."""
        frame = self.spot_base + int(stream_s * 1000 / FRAME_MS)
        return (self._now or time.time()) - (self.frame_no - frame) * FRAME_MS / 1000

    def _heard_until(self, now: float) -> float | None:
        rec = self.wake_rec
        end = getattr(rec, "speech_end", None) if rec is not None else None
        if not end:
            return None
        t = self._wall(end)
        return min(t, now)

    def _config(self, now: float) -> tuple[set[str], list[str], float, Any]:
        """What this source listens for, refreshed twice a second rather than every 30 ms frame:
        working it out deep-copies settings and agents and (for app rules) scans the window and
        process lists, which is too much to do ~33 times a second per source on the audio thread."""
        if self._cfg is None or now - self._cfg_at >= CONFIG_REFRESH:
            eng = self.engine
            modes = eng.detection_modes(self.source)
            spotter = eng.models.wake_spotter() if "vosk" in modes else None
            # every enabled agent's names, whether or not an app rule makes it active right now: the
            # grammar stays the same (rebuilding the recognizer drops whatever it was hearing), and a
            # name of an inactive agent is ignored when it's heard
            names = eng.wake_names(self.source) if spotter is not None else []
            eos = float(eng.settings.get("general.end_of_speech_seconds", 1.2))
            self._cfg, self._cfg_at = (modes, names, eos, spotter), now
            self._vad = float(eng.settings.get("audio.vad_threshold", 0.012))
            self._detector_setting = str(eng.settings.get("audio.speech_detector", "auto") or "auto")
        return self._cfg

    def threshold(self) -> float:
        if self._vad is not None:
            return speech_threshold(self.floor, self._vad)
        return speech_threshold(self.floor, float(self.engine.settings.get("audio.vad_threshold", 0.012)))

    def _wake_audio(self, spotter: Any, name: str) -> dict[str, Any]:
        """The audio of the call name itself (from just before it) and what came after it (with the
        speech detector's verdict on each frame)."""
        after = self._audio_after_name(spotter, name)
        after_voiced = list(self.voiced_ring)[-len(after):] if after else []
        words = getattr(spotter, "last_result", []) or []
        parts = name.split()
        start_s = None
        for i in range(len(words) - len(parts) + 1):
            if [w.get("word") for w in words[i:i + len(parts)]] == parts:
                start_s = float(words[i].get("start", 0))
        named: list[bytes] = []
        if start_s is not None:
            first = self.spot_base + int(max(0.0, start_s - NAME_PREROLL) * 1000 / FRAME_MS)
            back = self.frame_no - first
            if 0 < back <= len(self.ring):
                named = list(self.ring)[-back:]
                named = named[: max(0, len(named) - len(after))]      # up to where `after` begins
        return {"after": after, "name": named, "trailing": self.trailing, "after_voiced": after_voiced}

    def _audio_after_name(self, spotter: Any, name: str) -> list[bytes]:
        """Frames recorded after the call name in the phrase just recognised. Kept even when the wake
        model heard no words after the name: the name is reported mid-phrase, and the start of the
        request may already be in them. Whether they hold speech is decided by Engine._speech_in."""
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
