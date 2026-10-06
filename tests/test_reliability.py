"""Waking, answering and reading the screen reliably."""
import json
from pathlib import Path
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


# ---------------------------------------------------------------- screen: find, zoom in, answer
def _fake_screen(tmp_path):
    from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
    app = QGuiApplication.instance() or QGuiApplication([])       # noqa: F841
    img = QImage(1280, 400, QImage.Format_RGB32)
    img.fill(QColor(49, 51, 56))                                   # a dark chat...
    p = QPainter(img)
    p.fillRect(760, 0, 520, 400, QColor(255, 255, 255))            # ...beside a light settings page
    font = QFont("DejaVu Sans")
    font.setPixelSize(15)
    p.setFont(font)
    p.setPen(QColor(220, 222, 225))
    p.drawText(40, 60, "Dan")
    p.drawText(40, 84, "we should build the bridge tonight")
    p.drawText(40, 108, "bring the iron plates")
    p.setPen(QColor(30, 30, 30))
    p.drawText(800, 60, "Settings")
    p.drawText(800, 200, "Options and display scaling")
    p.end()
    path = tmp_path / "screen.png"
    img.save(str(path))
    return path


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")
def test_screen_reading_picks_the_relevant_part_and_reads_it_closely(engine, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from jeeves.daemon import desktop as dk
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import screen as sc
    src = _fake_screen(tmp_path)
    monkeypatch.setattr(sc, "screenshot", lambda: Path(shutil.copy(src, tmp_path / "shot.png")))
    monkeypatch.setattr(dk, "outputs", lambda: [])
    monkeypatch.setattr(dk, "focused", lambda: None)
    monkeypatch.setattr(engine.models, "vision_llm", lambda agent=None: None)
    prompts = []

    def respond(agent, prompt, system="", ctx=None, with_memory=False, raw=False):
        prompts.append(prompt)
        if "Which blocks" in prompt:
            line = next(ln for ln in prompt.splitlines() if "bridge" in ln)
            return line[1:line.index("]")]
        return "Dan wants to build the bridge tonight and says to bring iron plates."
    monkeypatch.setattr(engine.models, "respond", respond)
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("screen_reading", question="what did dan say")
    answer_prompt = prompts[-1]
    assert "build the bridge tonight" in answer_prompt and "iron plates" in answer_prompt
    assert "Options" not in answer_prompt                       # only the part that matters
    assert said == ["Dan wants to build the bridge tonight and says to bring iron plates."]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")
def test_screen_reading_never_reads_raw_text_when_the_model_gives_nothing(engine, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from jeeves.daemon import desktop as dk
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import screen as sc
    src = _fake_screen(tmp_path)
    monkeypatch.setattr(sc, "screenshot", lambda: Path(shutil.copy(src, tmp_path / "shot.png")))
    monkeypatch.setattr(dk, "outputs", lambda: [])
    monkeypatch.setattr(dk, "focused", lambda: None)
    monkeypatch.setattr(engine.models, "vision_llm", lambda agent=None: None)
    monkeypatch.setattr(engine.models, "respond", lambda *a, **k: None)
    said, shown = [], []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    monkeypatch.setattr(ctx, "show", lambda t: shown.append(t))
    ctx.call("screen_reading", question="read the screen")
    assert said and "bridge" not in said[-1] and "Options" not in said[-1]
    assert any("bridge" in t for t in shown)                     # the clean text is put up instead


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="needs tesseract")
def test_vision_model_gets_the_zoomed_in_crop(engine, monkeypatch, tmp_path):
    pytest.importorskip("PySide6")
    from jeeves.daemon import desktop as dk
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import screen as sc
    src = _fake_screen(tmp_path)
    monkeypatch.setattr(sc, "screenshot", lambda: Path(shutil.copy(src, tmp_path / "shot.png")))
    monkeypatch.setattr(dk, "outputs", lambda: [])
    monkeypatch.setattr(dk, "focused", lambda: None)
    seen = {}

    class Vision:
        def chat(self, messages, **kw):
            seen["messages"] = messages
            return "Dan says build the bridge tonight."
    monkeypatch.setattr(engine.models, "vision_llm", lambda agent=None: Vision())
    monkeypatch.setattr(engine.models, "respond", lambda agent, prompt, **k: next(
        ln[1:ln.index("]")] for ln in prompt.splitlines() if "bridge" in ln))
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("screen_reading", question="what did dan say")
    parts = seen["messages"][-1]["content"]
    assert [p["type"] for p in parts].count("image_url") == 1 and "bridge" in parts[-1]["text"]
    assert said == ["Dan says build the bridge tonight."]


def test_layout_groups_lines_into_blocks():
    from jeeves.functions.partials.screen import layout_blocks

    def word(text, x, y, line):
        return {"text": text, "x": x, "y": y, "w": 8 * len(text), "h": 14, "conf": 90, "line": line}
    words = [word("Dan", 40, 50, (1, 1, 1)), word("hello", 40, 72, (2, 1, 1)), word("there", 90, 72, (2, 1, 1)),
             word("Settings", 800, 50, (3, 1, 1)),                       # far to the right: its own block
             word("Blue", 40, 140, (4, 1, 1))]                           # a gap below: a new message
    blocks = layout_blocks(words)
    assert [b["text"] for b in blocks] == ["Dan\nhello there", "Settings", "Blue"]


# ---------------------------------------------------------------- research: stop at a real answer
def test_research_stops_at_the_page_that_answers_a_how_to(engine, monkeypatch):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import web
    monkeypatch.setattr(web, "search", lambda settings, q, n, problems=None: [
        {"title": "Wumpy - Parkour Reborn Wiki", "url": "https://w/wumpy", "snippet": ""},
        {"title": "How to wumpy - Parkour Reborn guide", "url": "https://w/guide", "snippet": ""},
        {"title": "Parkour Reborn history", "url": "https://w/history", "snippet": ""}])
    pages = {"https://w/wumpy": "A wumpy is an advanced wallrun technique. It was added in update 4. " * 10,
             "https://w/guide": "To do a wumpy: wallrun, jump off, then wallrun again within half a second. " * 10,
             "https://w/history": "Parkour Reborn was released in 2021. The wumpy was named after a player. " * 10}
    read, prompts = [], []
    monkeypatch.setattr(web, "request_website", lambda ctx, url, **kw: read.append(url) or pages[url])

    def respond(agent, prompt, **kw):
        prompts.append(prompt)
        if "web search queries" in prompt:
            return "wumpy parkour reborn"
        if "actually answer the question" in prompt:
            return "ANSWERED: 2" if "jump off" in prompt else "MORE"
        return "Wallrun, jump off, then wallrun again within half a second [2]."
    monkeypatch.setattr(engine.models, "respond", respond)
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("research", question="how do I do a wumpy in parkour reborn", depth="deep")
    assert "https://w/history" not in read                        # stopped once it had the steps
    assert [s["url"] for s in ctx.entry["sources"]] == ["https://w/guide"]
    assert "A page that only says what it is" in next(p for p in prompts if "actually answer" in p)
    final = prompts[-1]
    assert "give the steps in order" in final and "history" in final and "update 4" not in final
    assert said[-1] == "Wallrun, jump off, then wallrun again within half a second."


def test_research_without_an_answer_never_reads_a_snippet(engine, monkeypatch):
    from jeeves.daemon.context import FunctionContext
    from jeeves.daemon.history import new_entry
    from jeeves.functions.partials import web
    monkeypatch.setattr(web, "search", lambda settings, q, n, problems=None: [
        {"title": "Abilities", "url": "https://w/abilities", "snippet": "not to be confused with combat abilities"}])
    monkeypatch.setattr(web, "request_website", lambda ctx, url, **kw: "Abilities are obtained via books. " * 20)
    monkeypatch.setattr(engine.models, "respond", lambda *a, **k: None)
    said = []
    engine.speak = lambda ctx, t: said.append(t)
    ctx = FunctionContext(engine, "jeeves", engine.agents()["jeeves"], new_entry("x", "jeeves", "text"))
    ctx.call("research", question="what is a wumpy")
    assert "confused" not in said[-1] and "sources window" in said[-1]


# ---------------------------------------------------------------- web search fallbacks
def test_search_falls_back_when_duckduckgo_blocks(monkeypatch):
    from jeeves.functions.partials import web
    calls = []

    def ddg(q, n):
        calls.append("ddg")
        raise web.SearchBlocked("DuckDuckGo asked for a bot check")

    def mojeek(q, n):
        calls.append("mojeek")
        return [{"title": "Wumpy", "url": "https://w/wumpy", "snippet": "s"}]
    monkeypatch.setitem(web.ENGINES, "duckduckgo", ddg)
    monkeypatch.setitem(web.ENGINES, "mojeek", mojeek)
    problems = []

    class S:
        def get(self, k, d=None):
            return d
    assert web.search(S(), "wumpy", 5, problems)[0]["url"] == "https://w/wumpy"
    assert calls == ["ddg", "mojeek"] and "bot check" in problems[0]


def test_result_page_parsers():
    import base64
    from jeeves.functions.partials.web import parse_bing, parse_mojeek
    moj = ('<li class=" r1"><a class="ob" href="https://w/a">x</a><h2><a class="title" href="https://w/a">Wumpy | '
           'Wiki</a></h2><p class="i">w</p><p class="s">A <b>wumpy</b> is</p></li><li><h2><a class="title" '
           'href="https://w/b">Guide</a></h2><p class="s">steps</p></li>')
    assert parse_mojeek(moj, 5) == [{"title": "Wumpy | Wiki", "url": "https://w/a", "snippet": "A wumpy is"},
                                    {"title": "Guide", "url": "https://w/b", "snippet": "steps"}]
    u = "a1" + base64.urlsafe_b64encode(b"https://w/c").decode().rstrip("=")
    bing = (f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=1&amp;u={u}&amp;ntb=1">Wumpy</a>'
            '</h2><div class="b_caption"><p class="b_lineclamp2">how to</p></div></li>')
    assert parse_bing(bing, 5) == [{"title": "Wumpy", "url": "https://w/c", "snippet": "how to"}]


# ---------------------------------------------------------------- thinking models
def test_thinking_model_gets_room_and_a_second_try(monkeypatch):
    from jeeves.models.backends import LLM
    bodies = []

    def post(url, body, timeout=300):
        bodies.append(body)
        if len(bodies) == 1:          # thought through the whole budget, no answer
            return {"choices": [{"message": {"content": "", "reasoning_content": "We need to answer..."}}]}
        return {"choices": [{"message": {"content": "A wumpy is a double wallrun.", "reasoning_content": "ok"}}]}
    monkeypatch.setattr(backends, "_post_json", post)

    class Local(LLM):
        base = "http://x"
    llm = Local()
    assert llm.chat([{"role": "user", "content": "hi"}], max_tokens=512) == "A wumpy is a double wallrun."
    assert bodies[0]["max_tokens"] == 512 and bodies[1]["max_tokens"] > 4000
    llm.chat([{"role": "user", "content": "again"}], max_tokens=512)
    assert bodies[2]["max_tokens"] == 512 + LLM.REASONING_ALLOWANCE      # known thinker: room from the start


def test_reasoning_written_into_the_answer_is_never_spoken():
    from jeeves.models.backends import strip_thinking
    assert strip_thinking("<think>hmm, the user wants") == ""
    assert strip_thinking("<|channel|>analysis<|message|>We need to answer<|end|><|start|>assistant"
                          "<|channel|>final<|message|>It's a wallrun trick.") == "It's a wallrun trick."
    assert strip_thinking("<|channel|>analysis<|message|>We need to answer the user") == ""


# ---------------------------------------------------------------- the wake word, then nothing
def test_only_the_name_keeps_listening_instead_of_answering(engine, monkeypatch):
    from jeeves.daemon.listener import Session
    monkeypatch.setattr(engine.models, "stt", lambda agent=None: type("S", (), {
        "transcribe": lambda self, pcm, prompt="", language="en": "Jeeves."})())
    handled = []
    monkeypatch.setattr(engine, "handle_text", lambda *a, **k: handled.append(a))
    s = Session("microphone", "jeeves", "request")
    s.got_speech = True
    engine._finish_session(s)
    again = engine.sessions.get("microphone")
    assert not handled and again is not None and again.retried and again.agent_id == "jeeves"


def test_name_tail_alone_is_not_the_request(engine):
    loud = audio.array.array("h", [6000, -6000] * (audio.FRAME_SAMPLES // 2)).tobytes()
    quiet = b"\0" * audio.FRAME_BYTES
    engine._start_listen("microphone", "jeeves", "jeeves", [loud] * 8 + [quiet] * 6, None, 0)
    assert not engine.sessions["microphone"].got_speech
