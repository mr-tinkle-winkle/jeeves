"""Waking, answering and reading the screen reliably."""
import json
import shutil
import sys
import time

import pytest

from jeeves.daemon import audio
from jeeves.models import backends
from jeeves.models.backends import BackendError, WakeRecognizer
from jeeves.models.manager import ModelUnavailable


# ---------------------------------------------------------------- wake word mid-phrase
class FakeKaldi:
    """Plays back a script of partial results; never reaches an end of phrase on its own
    (as over steady background sound)."""

    def __init__(self, partials, final_words):
        self.partials, self.final_words = list(partials), final_words
        self.finalized = 0

    def SetWords(self, on):
        pass

    def AcceptWaveform(self, frame):
        return False

    def PartialResult(self):
        return json.dumps({"partial": self.partials.pop(0) if self.partials else ""})

    def FinalResult(self):
        self.finalized += 1
        return json.dumps({"result": self.final_words})


def recognizer(monkeypatch, partials, final_words):
    fake = FakeKaldi(partials, final_words)

    class Vosk:
        @staticmethod
        def KaldiRecognizer(model, rate, grammar):
            return fake
    monkeypatch.setattr(backends, "_vosk", lambda: Vosk)
    return WakeRecognizer(object(), ["jeeves"]), fake


def test_wake_fires_from_partial_without_waiting_for_silence(monkeypatch):
    words = [{"word": "jeeves", "conf": 0.91, "start": 1.0, "end": 1.4}]
    rec, fake = recognizer(monkeypatch, ["", "jeeves", "jeeves", "jeeves"], words)
    hits = []
    for _ in range(12):
        hits += rec.feed(b"\0" * 960)
    assert hits == [("jeeves", 0.91)] and fake.finalized == 1
    assert rec.last_result == words                 # timings kept for the audio after the name


def test_wake_fires_at_once_when_words_follow_the_name(monkeypatch):
    words = [{"word": "jeeves", "conf": 0.8, "start": 0.2, "end": 0.6}, {"word": "[unk]", "conf": 1, "start": 0.7,
                                                                          "end": 0.9}]
    rec, fake = recognizer(monkeypatch, ["jeeves [unk]"], words)
    assert rec.feed(b"") == [] and rec.feed(b"") == [("jeeves", 0.8)]   # first partial check: frame 2


def test_a_flicker_of_the_name_does_not_wake(monkeypatch):
    rec, fake = recognizer(monkeypatch, ["jeeves", "", "jeeves", ""], [])
    for _ in range(10):
        assert rec.feed(b"") == []
    assert fake.finalized == 0


def test_request_start_is_kept_after_an_early_wake(engine):
    loud = audio.array.array("h", [6000, -6000] * (audio.FRAME_SAMPLES // 2)).tobytes()
    engine._start_listen("microphone", "jeeves", "jeeves", [loud] * 4, None, 0)
    s = engine.sessions["microphone"]
    assert len(s.frames) == 4 and not s.got_speech    # kept, but not counted as the request yet


# ---------------------------------------------------------------- capture that stops
def test_stalled_capture_ends_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(audio, "capture_command", lambda src, kind: [sys.executable, "-c",
                                                                     "import time; time.sleep(30)"])
    monkeypatch.setattr(audio, "STALL_SECONDS", 0.3)
    cap = audio.Capture("", "desktop")
    t = time.time()
    try:
        assert list(cap.frames()) == []
        assert time.time() - t < 3
    finally:
        cap.close()


def test_capture_yields_frames(monkeypatch):
    script = f"import sys; sys.stdout.buffer.write(b'\\x01' * {audio.FRAME_BYTES * 3}); sys.stdout.flush()"
    monkeypatch.setattr(audio, "capture_command", lambda src, kind: [sys.executable, "-c", script])
    cap = audio.Capture("", "desktop")
    try:
        assert len(list(cap.frames())) == 3
    finally:
        cap.close()


# ---------------------------------------------------------------- answers that used to vanish
class FlakySTT:
    def __init__(self, failures):
        self.failures, self.unloads = failures, 0

    def transcribe(self, pcm, prompt="", language="en"):
        if self.failures:
            self.failures -= 1
            raise BackendError("whisper-server failed: timed out")
        return "set a timer for 2 minutes"

    def unload(self):
        self.unloads += 1


def test_speech_server_failure_is_retried(engine, monkeypatch):
    stt = FlakySTT(1)
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: stt)
    assert engine.transcribe(b"\0" * 960) == "set a timer for 2 minutes" and stt.unloads == 1


def test_speech_server_down_is_reported_not_lost(engine, monkeypatch):
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: FlakySTT(5))
    with pytest.raises(ModelUnavailable):
        engine.transcribe(b"\0" * 960)


def test_empty_model_answer_becomes_none(engine, monkeypatch):
    class Empty:
        model_name = "fake"

        def chat(self, *a, **k):
            return ""
    monkeypatch.setattr(engine.models, "llm", lambda kind, agent=None: Empty())
    monkeypatch.setattr(engine.models, "vision_llm", lambda agent=None: None)
    assert engine.models.respond({"name": "Jeeves"}, "hi") is None


# ---------------------------------------------------------------- reading the screen
def test_tsv_boxes_scaled_back_to_screenshot_pixels():
    from jeeves.functions.partials.screen import parse_tsv
    tsv = "level\tpage\tblock\tpar\tline\tword\tleft\ttop\twidth\theight\tconf\ttext\n" \
          "5\t1\t1\t1\t1\t1\t200\t100\t80\t30\t91.5\tSave\n5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t12\tjunk\n"
    assert parse_tsv(tsv, 2.0) == [{"text": "Save", "x": 100, "y": 50, "w": 40, "h": 15, "conf": 91.5,
                                    "line": ("1", "1", "1")}]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")
def test_light_text_on_dark_background_is_read(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
    from jeeves.functions.partials.screen import ocr_words, prepare_for_ocr
    app = QGuiApplication.instance() or QGuiApplication([])     # drawing text needs fonts  # noqa: F841
    img = QImage(900, 200, QImage.Format_RGB32)
    img.fill(QColor(30, 31, 34))                        # a dark theme...
    p = QPainter(img)
    p.fillRect(450, 0, 450, 200, QColor(250, 250, 250))  # ...next to a light page
    font = QFont("DejaVu Sans")
    font.setPixelSize(13)                               # ordinary UI text size
    p.setFont(font)
    p.setPen(QColor(200, 200, 205))
    p.drawText(20, 60, "Permission denied while saving")
    p.setPen(QColor(30, 30, 30))
    p.drawText(470, 60, "Release notes for version")
    p.end()
    shot = tmp_path / "shot.png"
    img.save(str(shot))
    prepared, scale = prepare_for_ocr(shot)
    prepared.unlink()
    assert scale == 2.0
    text = " ".join(w["text"] for w in ocr_words(shot)).lower()
    for word in ("permission", "denied", "saving", "release", "notes", "version"):
        assert word in text
    words = {w["text"].lower(): w for w in ocr_words(shot)}
    assert words["permission"]["x"] < 450 <= words["release"]["x"] < 900        # boxes in screenshot pixels
