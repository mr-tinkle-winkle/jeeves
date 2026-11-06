"""Model runtimes.

Large models run as local servers managed by the daemon, so "unload" really
frees the memory (the process exits) and "load" is starting it again:

* STT:  whisper.cpp ``whisper-server``          (or Vosk in-process)
* LLM:  llama.cpp ``llama-server`` (OpenAI-compatible API), or an external
        OpenAI-compatible endpoint you already run (Ollama, LM Studio)
* TTS:  ``piper`` / Kokoro (kokoro-onnx) / ``espeak-ng``
* Wake: Vosk with a grammar restricted to the agents' call names
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from ..daemon.audio import RATE, to_wav, wav_to_pcm
from .catalog import ModelEntry
from .download import env_threads, free_port, model_dir

log = logging.getLogger("jeeves.models")

# How fast the models actually ran, for the Logs page (Activity > Logs): the last calls of each kind.
import collections as _collections
STATS: dict[str, "_collections.deque[dict[str, Any]]"] = {"llm": _collections.deque(maxlen=40),
                                                         "stt": _collections.deque(maxlen=40)}


class BackendError(RuntimeError):
    pass


def _find(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# Minimum untouched CPU: model servers only run on these cores (None = all)
CPU_SET: set[int] | None = None


def _child_setup() -> None:
    """In each model server before it runs: only the allowed cores (Minimum untouched CPU), and no
    core dump when it crashes -- a crashed whisper/llama server is gigabytes, and systemd-coredump
    compressing one after another kept the CPU hot and systemd itself slow."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    if CPU_SET:
        try:
            os.sched_setaffinity(0, CPU_SET)
        except OSError:
            pass


_pin_cpus = _child_setup


class ManagedServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.cmd: list[str] = []
        self.started_at = 0.0
        self.load_seconds = 0.0
        # its own output: kept (the start, where it says which GPU and how many layers it put there, and
        # the last lines) for the Logs page, and read all the time -- a pipe nobody reads fills up, and
        # then the server stops dead the next time it writes
        import collections
        self.head: list[str] = []
        self.tail: collections.deque[str] = collections.deque(maxlen=60)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, cmd: list[str], health: str, timeout: float = 180.0) -> None:
        self.stop()
        log.info("starting %s: %s", self.name, " ".join(cmd))
        self.cmd, self.started_at = list(cmd), time.time()
        self.head, self.tail = [], type(self.tail)(maxlen=60)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, preexec_fn=_child_setup)
        reader = threading.Thread(target=self._drain, daemon=True, name=f"jeeves-{self.name}-output")
        reader.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                reader.join(timeout=1)
                err = "\n".join(list(self.tail)[-12:])
                self.proc = None
                raise BackendError(f"{self.name} exited while loading: {err.strip()[-800:]}")
            try:
                with urllib.request.urlopen(self.base + health, timeout=2) as r:
                    if r.status == 200:
                        self.load_seconds = time.time() - self.started_at
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.4)
        self.stop()
        raise BackendError(f"{self.name} didn't become ready in {timeout:.0f}s")

    def _drain(self) -> None:
        p = self.proc
        if p and p.stderr:
            for raw in iter(lambda: p.stderr.readline(), b""):
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                if len(self.head) < 120:
                    self.head.append(line)
                self.tail.append(line)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            self.proc = None


def _post_json(url: str, body: dict[str, Any], timeout: float = 300) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------

class STT:
    entry: ModelEntry

    def load(self) -> None: ...
    def unload(self) -> None: ...
    def loaded(self) -> bool: return True
    def transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str: raise NotImplementedError


_HELP: dict[str, str] = {}


def _supports(exe: str, flag: str) -> bool:
    """Whether this build of a program lists the option in its --help (asked once per program)."""
    if exe not in _HELP:
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10)
            _HELP[exe] = (out.stdout or "") + (out.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            _HELP[exe] = ""
    return flag in _HELP[exe]


class WhisperCppSTT(STT):
    def __init__(self, entry: ModelEntry, threads: int = 0, gpu: bool = False) -> None:
        self.entry = entry
        self.threads = threads
        self.gpu = gpu
        self.server = ManagedServer(f"whisper-server ({entry.id})")

    def load(self) -> None:
        exe = _find("whisper-server", "whisper-cpp-server")
        if not exe:
            raise BackendError("whisper.cpp isn't installed (whisper-server not found)")
        model = model_dir(self.entry) / self.entry.files[0].path
        self.server.port = free_port()
        # whisper.cpp gains little past 8 threads; all 28 of them on every few seconds of desktop audio
        # was most of Jeeves' CPU load (and heat), and a crash now and then
        threads = min(env_threads(self.threads), 8)
        extra = ["-ng"]
        if self.gpu:
            # on the graphics card, without flash attention: its lower-precision path is where GPU builds
            # of whisper.cpp recognize worse than the CPU (when this build has the switch)
            extra = ["-nfa"] if _supports(exe, "--no-flash-attn") else []
        self.server.start([exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(self.server.port),
                           "-t", str(threads)] + extra, "/")

    def unload(self) -> None:
        self.server.stop()

    def loaded(self) -> bool:
        return self.server.alive()

    busy = 0                                 # transcriptions in progress (the manager won't unload it meanwhile)

    def transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str:
        self.busy += 1
        t0 = time.time()
        ok = False
        try:
            text = self._transcribe(pcm, prompt, language)
            ok = True
            return text
        finally:
            self.busy -= 1
            took = time.time() - t0
            STATS["stt"].append({"at": t0, "model": getattr(getattr(self, "entry", None), "id", "?"),
                                 "audio_s": round(len(pcm) / 32000, 2), "took_s": round(took, 2),
                                 "speed": round((len(pcm) / 32000) / took, 1) if took > 0 else None,
                                 "ok": ok, "busy_at_once": self.busy + 1})

    def _transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str:
        if not self.loaded():
            self.load()
        fields = {"response_format": "verbose_json", "temperature": "0.0"}
        if prompt:
            fields["prompt"] = prompt
        if language and not self.entry.id.endswith("-en"):
            fields["language"] = language
        body, ctype = _multipart(fields, {"file": ("audio.wav", to_wav(pcm), "audio/wav")})
        req = urllib.request.Request(self.server.base + "/inference", data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
        except (OSError, ValueError, http.client.HTTPException) as exc:   # incl. timeouts, a crashed server
            raise BackendError(f"whisper-server failed: {exc}") from exc
        return speech_text(data)


def speech_text(data: dict[str, Any]) -> str:
    """Whisper's text without the segments it thinks were silence or noise (where it invents
    "Thank you." and the like): high no-speech probability with low confidence."""
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        return (data.get("text") or "").strip()
    keep = []
    for s in segs:
        text = (s.get("text") or "").strip()
        nsp = float(s.get("no_speech_prob", 0) or 0)
        lp = float(s.get("avg_logprob", 0) or 0)
        # only short, low-confidence bits flagged as silence -- never real sentences
        if nsp > 0.6 and lp < -0.5 and len(text.split()) <= 3:
            continue
        keep.append(text)
    return " ".join(t for t in keep if t).strip()


def _vosk():
    try:
        import vosk  # type: ignore
        vosk.SetLogLevel(-1)
        return vosk
    except Exception as exc:
        raise BackendError("Vosk isn't installed (python package 'vosk')") from exc


class VoskSTT(STT):
    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        self.model = None

    def load(self) -> None:
        self.model = _vosk().Model(str(model_dir(self.entry)))

    def unload(self) -> None:
        self.model = None

    def loaded(self) -> bool:
        return self.model is not None

    def transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str:
        if self.model is None:
            self.load()
        rec = _vosk().KaldiRecognizer(self.model, RATE)
        rec.AcceptWaveform(pcm)
        return json.loads(rec.FinalResult()).get("text", "").strip()


FRAME_SECONDS = 0.03          # the listeners feed 30 ms frames


class WakeRecognizer:
    """One streaming recognizer over a grammar of call names. Each listener (mic,
    desktop, a device) owns its own: Vosk recognizers aren't thread-safe, and sharing
    one between listener threads crashed the daemon."""

    PARTIAL_EVERY = 2        # frames between partial-result checks (~60 ms)
    PARTIAL_STABLE = 2       # checks in a row the name must stay in the partial result (~120 ms)

    # Words that compete with the names in the grammar. With only the names (and [unk]) to choose from,
    # Vosk decoded any short sound as the nearest name -- "guh", "gee", "cheese" came out as "jeeves" at
    # confidence 1.0. With these to pick instead, a "guh" is decoded as "uh" or "go", and a name has to
    # sound like the name. (Never the call names' own words; words the model doesn't know are dropped.)
    DECOYS = ("the a uh um huh oh ah eh he she we me hey hi yeah you g gee go good get guess just jeez geez "
              "cheese chief cheers sheets jeans genes keys please these eve eaves leaves believe believes "
              "reeves steve sleeves weaves achieves even easy").split()

    def __init__(self, model: Any, names: list[str], decoys: list[str] | None = None) -> None:
        self.model = model                  # keeps the shared model alive while in use
        self.names = names
        own = {w for n in names for w in n.split()}
        self.decoys = [w for w in dict.fromkeys(decoys or []) if w not in own]
        self.last_heard: list[tuple[str, float, float]] = []   # the words of the last phrase with a name
        self.rec = _vosk().KaldiRecognizer(model, RATE, json.dumps(names + self.decoys + ["[unk]"]))
        self.rec.SetWords(True)
        # (not SetPartialWords: with word times in partial results Vosk holds partials back by a second
        # or more, which is exactly what reporting the name early relies on)
        self.last_result: list[dict[str, Any]] = []
        self.phrase_open = False            # words decoded in the phrase so far (someone is talking)
        self.speech_end = 0.0               # stream time (s) speech was last decoded
        self._n = 0
        self._seen = 0
        self._last_partial = ""
        self.maybe = ""                     # a name just appeared in the partial result (not sure yet)

    UNSURE = 0.4             # confidence reported for a name only the partial result had

    def _name_in(self, tokens: list[str]) -> bool:
        return any(self._name_in_one(name, tokens) for name in self.names)

    @staticmethod
    def _name_in_one(name: str, tokens: list[str]) -> bool:
        parts = name.split()
        return any(tokens[i:i + len(parts)] == parts for i in range(len(tokens) - len(parts) + 1))

    def feed(self, frame: bytes) -> list[tuple[str, float]]:
        """Returns [(name, confidence)] detected in this frame.

        Vosk only finishes a phrase after a stretch of silence. Over steady background sound (a fan,
        music, a game, Discord) that silence may not come for many seconds, so the name was reported
        late or not at all. So the partial result is checked too: once a name has been in it for a
        moment (or more words already follow it), the phrase is finished right there."""
        self._n += 1
        if self.rec.AcceptWaveform(frame):
            self._seen = 0
            self.phrase_open = False
            self._last_partial = ""
            return self._found(json.loads(self.rec.Result()))
        if self._n % self.PARTIAL_EVERY:
            return []
        text = json.loads(self.rec.PartialResult()).get("partial", "")
        tokens = text.split()
        self.phrase_open = bool(tokens)
        if text and text != self._last_partial:
            self.speech_end = self._n * FRAME_SECONDS     # it just decoded more speech
        self._last_partial = text
        if not tokens or not self._name_in(tokens):
            self._seen = 0
            return []
        self._seen += 1
        if self._seen == 2:                 # the name stayed in it (~60 ms): the indicator shows -- not on
            # the first glimpse, which a "juh" or a "gee" gives too
            self.maybe = max((n for n in self.names if self._name_in_one(n, tokens)), key=len, default="")
        followed = not self._name_in(tokens[-1:]) and self._name_in(tokens[:-1])
        if self._seen < self.PARTIAL_STABLE + 1 and not followed:
            return []
        self._seen = 0
        # FinalResult ends the phrase now; word times keep counting from the stream's start
        found = self._found(json.loads(self.rec.FinalResult()))
        if not found:
            # the finished phrase came out without the name the partial result had (it happens most
            # over other people talking): unsure, so speech recognition gets to check it
            found = [(n, self.UNSURE) for n in self.names if self._name_in_one(n, tokens)][:1]
        return found

    def _found(self, res: dict[str, Any]) -> list[tuple[str, float]]:
        words = res.get("result", [])
        self.last_result = words      # with start/end times, for the audio after the name
        self.phrase_open = False
        self._last_partial = ""
        if words:
            self.speech_end = max(self.speech_end, max(float(w.get("end", 0)) for w in words))
        heard = [(w["word"], float(w.get("conf", 0)), float(w.get("end", 0)) - float(w.get("start", 0)))
                 for w in words]
        found = self.pick_names(heard, self.names)
        if found:
            self.last_heard = heard
            log.info("wake model heard %s -> %s", " ".join(f"{w}({c:.2f}, {d:.2f}s)" for w, c, d in heard),
                     ", ".join(f"{n} {c:.2f}" for n, c in found))
        return found

    @staticmethod
    def long_enough(word: str, seconds: float | None) -> float:
        """How fully a name word was said, 0..1, by how long it took. The grammar holds only the names, so
        Vosk maps any short sound onto one -- "g", "gee", even a "juh" came out as "jeeves" at confidence
        1.0, which is why no threshold setting helped. A real "Jeeves" takes ~0.3-0.5 s; a "juh" ~0.1."""
        if seconds is None:
            return 1.0
        expected = 0.05 + 0.045 * len(word)          # "jeeves": 0.32 s
        learned = WakeRecognizer.MIN_SECONDS        # (from your marked wake-ups: wakeclips.relearn_length)
        if learned and seconds < learned:
            return 0.2
        return max(0.0, min(1.0, seconds / expected)) ** 2

    MIN_SECONDS = 0.0

    EXTRA_WORD_CONF = 0.55   # "evil" in "evil jeeves" must be heard this clearly, or it's plain "jeeves"

    @classmethod
    def pick_names(cls, text_words: list[tuple[str, float]], names: list[str]) -> list[tuple[str, float]]:
        """The call names in a phrase, scored by the mean confidence of their words. One name inside
        another ("jeeves" in "evil jeeves") is one call, not two: the longer one when its extra words were
        heard clearly, else the shorter (the grammar only knows names, so "hey jeeves" can come out as
        "evil jeeves" -- with "evil" barely heard)."""
        spans: list[tuple[int, int, str, float]] = []
        # (word, confidence[, seconds]): a name word said too briefly to be it scores lower
        text_words = [(t[0], t[1] * cls.long_enough(t[0], t[2] if len(t) > 2 else None)) for t in text_words]
        for name in names:
            parts = name.split()
            for i in range(len(text_words) - len(parts) + 1):
                if [w for w, _ in text_words[i:i + len(parts)]] == parts:
                    conf = sum(c for _, c in text_words[i:i + len(parts)]) / len(parts)
                    spans.append((i, i + len(parts), name, conf))
        keep = []
        for a in spans:
            inner = [b for b in spans if b is not a and b[0] <= a[0] and a[1] <= b[1] and (b[1] - b[0]) > (a[1] - a[0])]
            outer = [b for b in spans if b is not a and a[0] <= b[0] and b[1] <= a[1] and (a[1] - a[0]) > (b[1] - b[0])]
            if inner:                      # a is inside a longer name: only if that one's extra words were unclear
                b = inner[0]
                extra = [c for j, (_, c) in enumerate(text_words[b[0]:b[1]]) if not (a[0] <= b[0] + j < a[1])]
                if extra and min(extra) >= cls.EXTRA_WORD_CONF:
                    continue
            if outer:                      # a contains a shorter name: only if its own extra words were clear
                b = outer[0]
                extra = [c for j, (_, c) in enumerate(text_words[a[0]:a[1]]) if not (b[0] <= a[0] + j < b[1])]
                if extra and min(extra) < cls.EXTRA_WORD_CONF:
                    continue
            keep.append((a[2], a[3]))
        return keep


class VoskWake:
    """The wake word model, loaded once and shared; listeners get their own recognizers."""

    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        self.model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        with self._lock:
            if self.model is None:
                self.model = _vosk().Model(str(model_dir(self.entry)))

    def unload(self) -> None:
        self.model = None             # recognizers still running keep their own reference

    def loaded(self) -> bool:
        return self.model is not None

    def quick_text(self, pcm: bytes) -> str:
        """Everything in a short stretch of audio, by the small wake model with its whole vocabulary: a
        rough transcript, but a fraction of a second of one CPU core where Whisper large on the CPU
        takes all of them for several seconds."""
        if self.model is None:
            self.load()
        rec = _vosk().KaldiRecognizer(self.model, RATE)
        rec.AcceptWaveform(pcm)
        return json.loads(rec.FinalResult()).get("text", "").strip()

    def recognizer(self, names: list[str]) -> WakeRecognizer:
        if self.model is None:
            self.load()
        names = sorted({n.lower() for n in names if n.strip()})
        self._warn_unknown(names)
        words = list(WakeRecognizer.DECOYS) + list(self.extra_decoys) if self.decoys else list(self.extra_decoys)
        return WakeRecognizer(self.model, names, self._known(words))

    decoys = True                           # wake_word.decoys (set by the model manager)
    extra_decoys: tuple[str, ...] = ()      # wake_word.extra_decoys: learned from wake-ups marked "not a call"
    grammar_version = 0                     # bumped when those change: listeners rebuild their recognizers

    def _known(self, words: list[str] | tuple[str, ...]) -> list[str]:
        find = getattr(self.model, "vosk_model_find_word", None) or getattr(self.model, "find_word", None)
        if find is None:
            return list(words)
        out = []
        for w in words:
            try:
                if find(w) >= 0:
                    out.append(w)
            except Exception:  # noqa: BLE001
                return list(words)
        return out

    _warned: set[str] = set()

    def _warn_unknown(self, names: list[str]) -> None:
        """Vosk silently drops grammar words it doesn't know, so a call name outside its vocabulary
        can never wake anything. Say so (once per word) instead of failing quietly."""
        find = getattr(self.model, "vosk_model_find_word", None) or getattr(self.model, "find_word", None)
        if find is None:
            return
        for word in {w for n in names for w in n.split()}:
            try:
                unknown = find(word) < 0
            except Exception:  # noqa: BLE001
                return
            if unknown and word not in self._warned:
                self._warned.add(word)
                log.warning("the wake word model doesn't know the word '%s', so call names using it can't "
                            "wake an agent; pick another call name or set wake_word.engine to stt-match", word)


# ---------------------------------------------------------------------------
# Text models
# ---------------------------------------------------------------------------

class LLM:
    entry: ModelEntry

    def load(self) -> None: ...
    def unload(self) -> None: ...
    def loaded(self) -> bool: return True

    @property
    def base(self) -> str:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        return "local"

    REASONING_ALLOWANCE = 2048    # extra tokens a thinking model gets to think before it answers
    thinks = False                # seen reasoning from this model: give it the allowance from now on

    def extra_body(self, think: bool | str | None = None) -> dict[str, Any]:
        """Server-specific request fields (llama-server's chat template switches)."""
        return {}

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 512, temperature: float = 0.6,
             json_mode: bool = False, on_token: Callable[[str], None] | None = None,
             cancelled: Callable[[], bool] | None = None, think: bool | str | None = None) -> str:
        """The answer. max_tokens is for the answer itself: llama-server counts a thinking model's
        reasoning against it too, so gpt-oss/Qwen3 could think through the whole budget and answer
        nothing (the empty answers). Thinking models get an allowance on top, and an answer that
        still comes back empty after thinking is asked for once more with plenty of room."""
        if not self.loaded():
            self.load()
        budget = max_tokens + (self.REASONING_ALLOWANCE * (2 if think == "high" else 1) if self.thinks or think
                               else 0)
        t0 = time.time()
        self._last_timings = None
        content, thought = self._chat_once(messages, budget, temperature, json_mode, on_token, cancelled, think)
        took = time.time() - t0
        tm = self._last_timings or {}
        STATS["llm"].append({"at": t0, "model": self.model_name, "took_s": round(took, 2), "think": bool(think),
                             "prompt_chars": sum(len(str(m.get("content", ""))) for m in messages),
                             "answer_chars": len(content), "thought_chars": len(thought),
                             "prompt_tokens": tm.get("prompt_n"), "made_tokens": tm.get("predicted_n"),
                             "read_tok_s": round(tm["prompt_per_second"], 1) if tm.get("prompt_per_second") else None,
                             "write_tok_s": round(tm["predicted_per_second"], 1) if tm.get("predicted_per_second")
                             else None, "stream": on_token is not None})
        if thought:
            self.thinks = True
        if not content and thought and not (cancelled and cancelled()):
            log.info("%s thought without answering (%d tokens allowed); asking again with more room",
                     self.model_name, budget)
            if on_token is not None:
                on_token("\n[ran out of room while thinking -- trying again]\n")
            content, _ = self._chat_once(messages, max_tokens + 3 * self.REASONING_ALLOWANCE, temperature,
                                         json_mode, on_token, cancelled, think)
        return content

    def _chat_once(self, messages: list[dict[str, Any]], max_tokens: int, temperature: float, json_mode: bool,
                   on_token: Callable[[str], None] | None,
                   cancelled: Callable[[], bool] | None, think: bool | str | None = None) -> tuple[str, str]:
        """(answer, reasoning) for one request."""
        body: dict[str, Any] = {"model": self.model_name, "messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature, **self.extra_body(think)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if on_token is None:
            try:
                data = _post_json(self.base + "/v1/chat/completions", body)
                self._last_timings = data.get("timings")          # (llama-server: tokens per second)
                msg = data["choices"][0]["message"]
            except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException) as exc:
                raise BackendError(f"model server failed: {exc}") from exc
            raw = msg.get("content") or ""
            return strip_thinking(raw), (msg.get("reasoning_content") or "") + thinking_part(raw)
        body["stream"] = True
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        out: list[str] = []
        thoughts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                for raw_line in r:
                    if cancelled and cancelled():
                        break
                    line = raw_line.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                        if chunk.get("timings"):
                            self._last_timings = chunk["timings"]
                        d = chunk["choices"][0].get("delta", {})
                    except (ValueError, KeyError, IndexError):
                        continue
                    # a thinking model's reasoning: shown in the thoughts view, never spoken
                    thought = d.get("reasoning_content") or ""
                    if thought:
                        thoughts.append(thought)
                        on_token(thought)
                    delta = d.get("content") or ""
                    if delta:
                        out.append(delta)
                        on_token(delta)
        except (OSError, http.client.HTTPException) as exc:     # incl. read timeouts and a crashed server
            raise BackendError(f"model server failed: {exc}") from exc
        raw = "".join(out)
        return strip_thinking(raw), "".join(thoughts) + thinking_part(raw)


def thinking_part(text: str) -> str:
    """Reasoning a model wrote into the answer itself (<think>, or gpt-oss's analysis channel)."""
    import re
    parts = re.findall(r"<think>(.*?)(?:</think>|$)", text, flags=re.S)
    parts += re.findall(r"<\|channel\|>analysis<\|message\|>(.*?)(?:<\|end\|>|<\|start\|>|$)", text, flags=re.S)
    return "".join(parts)


def strip_thinking(text: str) -> str:
    """Drop reasoning some models put in the answer itself: <think>...</think> blocks, an unfinished
    <think> (it ran out of tokens mid-thought), and gpt-oss's harmony channels when the server
    didn't separate them (only the final channel is the answer)."""
    import re
    if "<|channel|>" in text:
        m = re.search(r"<\|channel\|>final<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)", text, flags=re.S)
        text = m.group(1) if m else ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:                 # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
    text = re.sub(r"<think>.*$", "", text, flags=re.S)
    return text.strip()


class LlamaCppLLM(LLM):
    def __init__(self, entry: ModelEntry, threads: int = 0, gpu_layers: Any = 0, ctx_size: int = 8192,
                 reasoning: str = "off") -> None:
        self.entry = entry
        self.threads, self.gpu_layers, self.ctx_size = threads, gpu_layers, ctx_size
        self.reasoning = reasoning
        self.server = ManagedServer(f"llama-server ({entry.id})")

    @property
    def base(self) -> str:
        return self.server.base

    def load(self) -> None:
        exe = _find("llama-server")
        if not exe:
            raise BackendError("llama.cpp isn't installed (llama-server not found)")
        model = model_dir(self.entry) / self.entry.files[0].path
        self.server.port = free_port()
        cmd = [exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(self.server.port), "-c",
               str(self.ctx_size), "-t", str(env_threads(self.threads))]
        # gpu_layers: a number, or a function(model_path, ctx_size) -> number (Auto)
        ngl = self.gpu_layers(model, self.ctx_size) if callable(self.gpu_layers) else int(self.gpu_layers or 0)
        cmd += ["-ngl", str(ngl)]
        mmproj = model_dir(self.entry) / "mmproj.gguf"
        if mmproj.exists():                           # vision models: can be sent screenshots
            cmd += ["--mmproj", str(mmproj)]
        # thinking models (Qwen3, gpt-oss): off = answer straight away (fast); auto = let them think
        cmd += ["--reasoning", self.reasoning if self.reasoning in ("on", "off", "auto") else "off"]
        self.server.start(cmd, "/health")

    def unload(self) -> None:
        self.server.stop()

    def loaded(self) -> bool:
        return self.server.alive()

    def extra_body(self, think: bool | str | None = None) -> dict[str, Any]:
        # per request (the Jeenius scale): Qwen3 and DeepSeek read enable_thinking, gpt-oss reasoning_effort
        # (it can't stop thinking altogether: "no" means as little as it can). Other templates ignore both.
        if think is None:
            think = self.reasoning in ("on", "auto")
        effort = "high" if think == "high" else "medium" if think else "low"
        return {"chat_template_kwargs": {"reasoning_effort": effort, "enable_thinking": bool(think)}}


class EndpointLLM(LLM):
    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        url, _, name = entry.id[9:].partition("|")
        self._base = url.rstrip("/").removesuffix("/v1")
        self._name = name or "default"

    @property
    def base(self) -> str:
        return self._base

    @property
    def model_name(self) -> str:
        return self._name


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------

class TTS:
    def load(self) -> None: ...
    def unload(self) -> None: ...
    def loaded(self) -> bool: return True
    def synth(self, text: str, voice: ModelEntry | None,
              style: dict[str, Any] | None = None) -> tuple[bytes, int]: raise NotImplementedError


def speaker_map(voice: ModelEntry) -> dict[str, int]:
    """A downloaded Piper voice's speakers: name -> id (empty for single-speaker voices)."""
    try:
        cfg = json.loads((model_dir(voice) / "voice.onnx.json").read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): int(v) for k, v in (cfg.get("speaker_id_map") or {}).items()}


def speaker_id(voice: ModelEntry, wanted: str | int | None) -> int | None:
    """The id for a speaker name/number; None = the model's default."""
    want = str(wanted if wanted not in (None, "") else (voice.speaker or "")).strip()
    if not want:
        return None
    m = speaker_map(voice)
    if want in m:
        return m[want]
    low = {k.lower(): v for k, v in m.items()}
    if want.lower() in low:
        return low[want.lower()]
    if want.isdigit() and (not m or int(want) in m.values() or int(want) < max(voice.speakers, 1)):
        return int(want)
    return None


class EspeakTTS(TTS):
    def synth(self, text: str, voice: ModelEntry | None,
              style: dict[str, Any] | None = None) -> tuple[bytes, int]:
        exe = _find("espeak-ng", "espeak")
        if not exe:
            raise BackendError("espeak-ng isn't installed")
        st = style or {}
        v = voice.voice if voice and voice.engine == "espeak-ng" else "en-us"
        cmd = [exe, "-v", v or "en-us", "-s", str(int(175 * float(st.get("speed", 1.0)))),
               "-p", str(int(max(0, min(99, 50 + float(st.get("pitch", 0)) * 4)))), "--stdout", text]
        out = subprocess.run(cmd, capture_output=True, timeout=60)
        if out.returncode != 0:
            raise BackendError(out.stderr.decode(errors="replace")[:200])
        return wav_to_pcm(out.stdout)


class PiperTTS(TTS):
    def synth(self, text: str, voice: ModelEntry | None,
              style: dict[str, Any] | None = None) -> tuple[bytes, int]:
        exe = _find("piper", "piper-tts")
        if not exe:
            raise BackendError("piper isn't installed")
        if voice is None or voice.engine != "piper":
            raise BackendError("pick a Piper voice")
        d = model_dir(voice)
        try:
            rate = int(json.loads((d / "voice.onnx.json").read_text())["audio"]["sample_rate"])
        except (OSError, ValueError, KeyError):
            rate = 22050
        st = style or {}
        cmd = [exe, "--model", str(d / "voice.onnx"), "--output_raw",
               "--length_scale", f"{1.0 / max(0.5, float(st.get('speed', 1.0))):.3f}",
               "--noise_scale", f"{float(st.get('expressiveness', 0.667)):.3f}"]
        sid = speaker_id(voice, st.get("speaker"))
        if sid is not None:
            cmd += ["--speaker", str(sid)]
        out = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=120)
        if out.returncode != 0:
            raise BackendError(out.stderr.decode(errors="replace")[-300:])
        return out.stdout, rate


class KokoroTTS(TTS):
    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        self.k = None
        self._lock = threading.Lock()     # one synthesis at a time (espeak-ng has global state)

    def load(self) -> None:
        try:
            from kokoro_onnx import Kokoro  # type: ignore
        except Exception as exc:
            raise BackendError("Kokoro isn't installed (python package 'kokoro-onnx')") from exc
        d = model_dir(self.entry)
        self.k = Kokoro(str(d / "kokoro-v1.0.onnx"), str(d / "voices-v1.0.bin"))

    def unload(self) -> None:
        self.k = None

    def loaded(self) -> bool:
        return self.k is not None

    def synth(self, text: str, voice: ModelEntry | None,
              style: dict[str, Any] | None = None) -> tuple[bytes, int]:
        with self._lock:
            return self._synth(text, voice, style or {})

    def _synth(self, text: str, voice: ModelEntry | None, st: dict[str, Any]) -> tuple[bytes, int]:
        if self.k is None:
            self.load()
        v = voice.voice if voice and voice.engine == "kokoro" else "af_heart"
        use: Any = v
        blend = str(st.get("blend") or "").removeprefix("kokoro-")
        amount = float(st.get("blend_amount", 0.3))
        if blend and blend != v and 0 < amount:
            try:                                  # mix two voices: a new voice of your own
                getter = getattr(self.k, "get_voice_style", None)
                if getter is None:                # (older kokoro-onnx: the voices table directly)
                    getter = lambda name: self.k.voices[name]  # noqa: E731
                use = getter(v) * (1 - amount) + getter(blend) * amount
            except Exception as exc:  # noqa: BLE001 -- unknown voice: just the main one, and say so
                log.warning("couldn't mix %s into %s (%s); using %s alone", blend, v, exc, v)
                use = v
        speed = max(0.5, min(2.0, float(st.get("speed", 1.0))))
        samples, rate = self.k.create(text, voice=use, speed=speed, lang="en-gb" if v.startswith("b") else "en-us")
        import array
        pcm = array.array("h", (max(-32768, min(32767, int(s * 32767))) for s in samples)).tobytes()
        return pcm, int(rate)


def make_stt(entry: ModelEntry, threads: int, gpu: bool = True) -> STT:
    if entry.engine == "whisper.cpp":
        return WhisperCppSTT(entry, threads, gpu)
    if entry.engine == "vosk":
        return VoskSTT(entry)
    raise BackendError(f"{entry.id} isn't a speech-to-text model")


def make_llm(entry: ModelEntry, threads: int, gpu_layers: Any, reasoning: str = "off") -> LLM:
    if entry.engine == "llama.cpp":
        return LlamaCppLLM(entry, threads, gpu_layers, reasoning=reasoning)
    if entry.engine == "endpoint":
        return EndpointLLM(entry)
    raise BackendError(f"{entry.id} isn't a text model")


def make_tts(entry: ModelEntry) -> TTS:
    if entry.engine == "espeak-ng":
        return EspeakTTS()
    if entry.engine == "piper":
        return PiperTTS()
    if entry.engine == "kokoro":
        return KokoroTTS(entry)
    raise BackendError(f"{entry.id} isn't a TTS engine")


__all__ = ["BackendError", "make_stt", "make_llm", "make_tts", "VoskWake", "STT", "LLM", "TTS"]
