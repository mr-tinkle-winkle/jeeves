"""Is this 30 ms of audio someone speaking?

The level meter (louder than the background?) can't hear a voice over music or a game -- a voice
barely changes the level there -- and takes every key press and mouse click for speech, so a
listen ended before the request or never ended at all. Silero VAD, a small neural speech
detector (1.3 MB, about a tenth of a millisecond per frame), tells speech from music, noise and
clicks. It needs onnxruntime (which the Kokoro voices use too); without it the level meter is used.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("jeeves.vad")

CHUNK = 512              # samples per model step at 16 kHz (32 ms)
CONTEXT = 64             # samples of the previous step the model sees again
ON, OFF = 0.5, 0.35      # speech starts above ON and lasts until it drops below OFF

_lock = threading.Lock()
_session: Any = None
_failed: str | None = None


def _model() -> Any:
    """The model, loaded once and shared (each stream keeps its own memory)."""
    global _session, _failed
    with _lock:
        if _session is None and _failed is None:
            try:
                import numpy  # noqa: F401
                import onnxruntime as ort  # type: ignore
                from ..resources import VAD_MODEL
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 1
                opts.log_severity_level = 3
                _session = ort.InferenceSession(str(VAD_MODEL), sess_options=opts,
                                                providers=["CPUExecutionProvider"])
            except Exception as exc:  # noqa: BLE001 -- the level meter takes over
                _failed = str(exc) or exc.__class__.__name__
                log.warning("speech detector unavailable (%s); using the level meter", _failed)
        return _session


def available() -> bool:
    return _model() is not None


class SpeechDetector:
    """One audio stream's speech detector."""

    def __init__(self, session: Any) -> None:
        import numpy as np
        self.np = np
        self.session = session
        self.state = np.zeros((2, 1, 128), np.float32)
        self.context = np.zeros(CONTEXT, np.float32)
        self.pending = np.zeros(0, np.float32)
        self.rate = np.array(16000, dtype=np.int64)
        self.prob = 0.0
        self.speaking = False

    @classmethod
    def create(cls) -> "SpeechDetector | None":
        session = _model()
        return cls(session) if session is not None else None

    def feed(self, frame: bytes) -> bool:
        """Takes the next frame (16 kHz mono s16le); True while speech is going on."""
        np = self.np
        x = np.frombuffer(frame[: len(frame) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        self.pending = np.concatenate([self.pending, x]) if len(self.pending) else x
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            out, self.state = self.session.run(
                None, {"input": np.concatenate([self.context, chunk])[None, :], "state": self.state,
                       "sr": self.rate})
            self.context = chunk[-CONTEXT:]
            p = float(out[0, 0])
            self.prob = p if p == p else 0.0          # (NaN guard)
        self.speaking = self.prob > ON or (self.speaking and self.prob > OFF)
        return self.speaking
