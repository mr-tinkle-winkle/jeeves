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


def _pin_cpus() -> None:
    if CPU_SET:
        try:
            os.sched_setaffinity(0, CPU_SET)
        except OSError:
            pass


class ManagedServer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.proc: subprocess.Popen | None = None
        self.port = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, cmd: list[str], health: str, timeout: float = 180.0) -> None:
        self.stop()
        log.info("starting %s: %s", self.name, " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                     preexec_fn=_pin_cpus if CPU_SET else None)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() or b"").decode(errors="replace")[-800:] if self.proc.stderr else ""
                self.proc = None
                raise BackendError(f"{self.name} exited while loading: {err.strip()}")
            try:
                with urllib.request.urlopen(self.base + health, timeout=2) as r:
                    if r.status == 200:
                        # stop collecting stderr into a pipe nobody reads
                        threading.Thread(target=self._drain, daemon=True).start()
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.4)
        self.stop()
        raise BackendError(f"{self.name} didn't become ready in {timeout:.0f}s")

    def _drain(self) -> None:
        p = self.proc
        if p and p.stderr:
            for _ in iter(lambda: p.stderr.readline(), b""):
                pass

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
        self.server.start([exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(self.server.port),
                           "-t", str(env_threads(self.threads))] + ([] if self.gpu else ["-ng"]), "/")

    def unload(self) -> None:
        self.server.stop()

    def loaded(self) -> bool:
        return self.server.alive()

    def transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str:
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


class WakeRecognizer:
    """One streaming recognizer over a grammar of call names. Each listener (mic,
    desktop, a device) owns its own: Vosk recognizers aren't thread-safe, and sharing
    one between listener threads crashed the daemon."""

    PARTIAL_EVERY = 2        # frames between partial-result checks (~60 ms)
    PARTIAL_STABLE = 3       # checks in a row the name must stay in the partial result (~180 ms)

    def __init__(self, model: Any, names: list[str]) -> None:
        self.model = model                  # keeps the shared model alive while in use
        self.names = names
        self.rec = _vosk().KaldiRecognizer(model, RATE, json.dumps(names + ["[unk]"]))
        self.rec.SetWords(True)
        self.last_result: list[dict[str, Any]] = []
        self._n = 0
        self._seen = 0

    def _name_in(self, tokens: list[str]) -> bool:
        for name in self.names:
            parts = name.split()
            if any(tokens[i:i + len(parts)] == parts for i in range(len(tokens) - len(parts) + 1)):
                return True
        return False

    def feed(self, frame: bytes) -> list[tuple[str, float]]:
        """Returns [(name, confidence)] detected in this frame.

        Vosk only finishes a phrase after a stretch of silence. Over steady background sound (a fan,
        music, a game, Discord) that silence may not come for many seconds, so the name was reported
        late or not at all. So the partial result is checked too: once a name has been in it for a
        moment (or more words already follow it), the phrase is finished right there."""
        if self.rec.AcceptWaveform(frame):
            self._seen = 0
            return self._found(json.loads(self.rec.Result()))
        self._n += 1
        if self._n % self.PARTIAL_EVERY:
            return []
        tokens = json.loads(self.rec.PartialResult()).get("partial", "").split()
        if not tokens or not self._name_in(tokens):
            self._seen = 0
            return []
        self._seen += 1
        followed = not self._name_in(tokens[-1:]) and self._name_in(tokens[:-1])
        if self._seen < self.PARTIAL_STABLE and not followed:
            return []
        self._seen = 0
        # FinalResult ends the phrase now; word times keep counting from the stream's start
        return self._found(json.loads(self.rec.FinalResult()))

    def _found(self, res: dict[str, Any]) -> list[tuple[str, float]]:
        words = res.get("result", [])
        self.last_result = words      # with start/end times, for the audio after the name
        found = []
        # multi-word call names: score = mean confidence of their words in sequence
        text_words = [(w["word"], float(w.get("conf", 0))) for w in words]
        for name in self.names:
            parts = name.split()
            for i in range(len(text_words) - len(parts) + 1):
                if [w for w, _ in text_words[i:i + len(parts)]] == parts:
                    conf = sum(c for _, c in text_words[i:i + len(parts)]) / len(parts)
                    found.append((name, conf))
        return found


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

    def recognizer(self, names: list[str]) -> WakeRecognizer:
        if self.model is None:
            self.load()
        names = sorted({n.lower() for n in names if n.strip()})
        self._warn_unknown(names)
        return WakeRecognizer(self.model, names)

    _warned: set[str] = set()

    def _warn_unknown(self, names: list[str]) -> None:
        """Vosk silently drops grammar words it doesn't know, so a call name outside its vocabulary
        can never wake anything. Say so (once per word) instead of failing quietly."""
        find = getattr(self.model, "find_word", None)
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

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 512, temperature: float = 0.6,
             json_mode: bool = False, on_token: Callable[[str], None] | None = None,
             cancelled: Callable[[], bool] | None = None) -> str:
        if not self.loaded():
            self.load()
        body: dict[str, Any] = {"model": self.model_name, "messages": messages, "max_tokens": max_tokens,
                                "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if on_token is None:
            try:
                data = _post_json(self.base + "/v1/chat/completions", body)
            except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
                raise BackendError(f"model server failed: {exc}") from exc
            return strip_thinking(data["choices"][0]["message"].get("content") or "")
        body["stream"] = True
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        out = []
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                for raw in r:
                    if cancelled and cancelled():
                        break
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        d = json.loads(payload)["choices"][0].get("delta", {})
                    except (ValueError, KeyError, IndexError):
                        continue
                    # a thinking model's reasoning: shown in the thoughts view, never spoken
                    thought = d.get("reasoning_content") or ""
                    if thought:
                        on_token(thought)
                    delta = d.get("content") or ""
                    if delta:
                        out.append(delta)
                        on_token(delta)
        except (OSError, http.client.HTTPException) as exc:     # incl. read timeouts and a crashed server
            raise BackendError(f"model server failed: {exc}") from exc
        return strip_thinking("".join(out))


def strip_thinking(text: str) -> str:
    """Drop <think>...</think> blocks some models put in the answer itself."""
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    if "</think>" in text:                 # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
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
                use = self.k.get_voice_style(v) * (1 - amount) + self.k.get_voice_style(blend) * amount
            except Exception:  # noqa: BLE001 -- unknown voice: just the main one
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
