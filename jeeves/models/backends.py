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

import json
import logging
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
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
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
        self.server = ManagedServer(f"whisper-server ({entry.id})")

    def load(self) -> None:
        exe = _find("whisper-server", "whisper-cpp-server")
        if not exe:
            raise BackendError("whisper.cpp isn't installed (whisper-server not found)")
        model = model_dir(self.entry) / self.entry.files[0].path
        self.server.port = free_port()
        self.server.start([exe, "-m", str(model), "--host", "127.0.0.1", "--port", str(self.server.port),
                           "-t", str(env_threads(self.threads))], "/")

    def unload(self) -> None:
        self.server.stop()

    def loaded(self) -> bool:
        return self.server.alive()

    def transcribe(self, pcm: bytes, prompt: str = "", language: str = "en") -> str:
        if not self.loaded():
            self.load()
        fields = {"response_format": "json", "temperature": "0.0"}
        if prompt:
            fields["prompt"] = prompt
        if language and not self.entry.id.endswith("-en"):
            fields["language"] = language
        body, ctype = _multipart(fields, {"file": ("audio.wav", to_wav(pcm), "audio/wav")})
        req = urllib.request.Request(self.server.base + "/inference", data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
        except (urllib.error.URLError, ValueError) as exc:
            raise BackendError(f"whisper-server failed: {exc}") from exc
        return (data.get("text") or "").strip()


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


class VoskWake:
    """Streaming keyword spotter over a grammar of call names."""

    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        self.model = None
        self.rec = None
        self.names: list[str] = []
        self.last_result: list[dict[str, Any]] = []

    def load(self) -> None:
        self.model = _vosk().Model(str(model_dir(self.entry)))

    def unload(self) -> None:
        self.model = self.rec = None

    def loaded(self) -> bool:
        return self.model is not None

    def set_names(self, names: list[str]) -> None:
        names = sorted({n.lower() for n in names if n.strip()})
        if names == self.names and self.rec is not None:
            return
        self.names = names
        if self.model is None:
            self.load()
        grammar = json.dumps(names + ["[unk]"])
        self.rec = _vosk().KaldiRecognizer(self.model, RATE, grammar)
        self.rec.SetWords(True)

    def feed(self, frame: bytes) -> list[tuple[str, float]]:
        """Returns [(name, confidence)] detected in this frame (end of a phrase)."""
        if self.rec is None:
            return []
        if not self.rec.AcceptWaveform(frame):
            return []
        res = json.loads(self.rec.Result())
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
            except (urllib.error.URLError, ValueError) as exc:
                raise BackendError(f"model server failed: {exc}") from exc
            return data["choices"][0]["message"]["content"] or ""
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
                        delta = json.loads(payload)["choices"][0].get("delta", {}).get("content") or ""
                    except (ValueError, KeyError, IndexError):
                        continue
                    if delta:
                        out.append(delta)
                        on_token(delta)
        except urllib.error.URLError as exc:
            raise BackendError(f"model server failed: {exc}") from exc
        return "".join(out)


class LlamaCppLLM(LLM):
    def __init__(self, entry: ModelEntry, threads: int = 0, gpu_layers: int = 0, ctx_size: int = 8192) -> None:
        self.entry = entry
        self.threads, self.gpu_layers, self.ctx_size = threads, gpu_layers, ctx_size
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
        if self.gpu_layers:
            cmd += ["-ngl", str(self.gpu_layers)]
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
    def synth(self, text: str, voice: ModelEntry | None) -> tuple[bytes, int]: raise NotImplementedError


class EspeakTTS(TTS):
    def synth(self, text: str, voice: ModelEntry | None) -> tuple[bytes, int]:
        exe = _find("espeak-ng", "espeak")
        if not exe:
            raise BackendError("espeak-ng isn't installed")
        v = voice.voice if voice and voice.engine == "espeak-ng" else "en-us"
        out = subprocess.run([exe, "-v", v or "en-us", "--stdout", text], capture_output=True, timeout=60)
        if out.returncode != 0:
            raise BackendError(out.stderr.decode(errors="replace")[:200])
        return wav_to_pcm(out.stdout)


class PiperTTS(TTS):
    def synth(self, text: str, voice: ModelEntry | None) -> tuple[bytes, int]:
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
        out = subprocess.run([exe, "--model", str(d / "voice.onnx"), "--output_raw"], input=text.encode(),
                             capture_output=True, timeout=120)
        if out.returncode != 0:
            raise BackendError(out.stderr.decode(errors="replace")[-300:])
        return out.stdout, rate


class KokoroTTS(TTS):
    def __init__(self, entry: ModelEntry) -> None:
        self.entry = entry
        self.k = None

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

    def synth(self, text: str, voice: ModelEntry | None) -> tuple[bytes, int]:
        if self.k is None:
            self.load()
        v = voice.voice if voice and voice.engine == "kokoro" else "af_heart"
        samples, rate = self.k.create(text, voice=v, speed=1.0, lang="en-us" if v.startswith("a") else "en-gb")
        import array
        pcm = array.array("h", (max(-32768, min(32767, int(s * 32767))) for s in samples)).tobytes()
        return pcm, int(rate)


def make_stt(entry: ModelEntry, threads: int) -> STT:
    if entry.engine == "whisper.cpp":
        return WhisperCppSTT(entry, threads)
    if entry.engine == "vosk":
        return VoskSTT(entry)
    raise BackendError(f"{entry.id} isn't a speech-to-text model")


def make_llm(entry: ModelEntry, threads: int, gpu_layers: int) -> LLM:
    if entry.engine == "llama.cpp":
        return LlamaCppLLM(entry, threads, gpu_layers)
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
