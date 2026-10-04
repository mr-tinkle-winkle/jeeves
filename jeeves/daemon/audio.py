"""Audio in and out through PipeWire/PulseAudio command-line tools, so the
daemon needs no native audio bindings.

Capture: ``pw-record`` (PipeWire), else ``parec`` (Pulse), else ``arecord``.
         16 kHz mono signed 16-bit, read in 30 ms frames.
Desktop audio is captured from the default sink's monitor.
Playback: ``pw-play`` / ``paplay`` / ``aplay``; raw PCM or files.
"Speak through the microphone" plays into a null sink named ``jeeves-mic``
whose monitor shows up as a microphone other apps (Discord, OBS) can select.
"""
from __future__ import annotations

import array
import io
import logging
import math
import os
import shutil
import subprocess
import threading
import wave
from pathlib import Path
from typing import Callable, Iterator

from ..util import which

log = logging.getLogger("jeeves.audio")

RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * 2


def rms(frame: bytes) -> float:
    if not frame:
        return 0.0
    a = array.array("h")
    a.frombytes(frame[: len(frame) - len(frame) % 2])
    if not a:
        return 0.0
    return math.sqrt(sum(s * s for s in a) / len(a)) / 32768.0


def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def capture_command(source: str, kind: str) -> list[str]:
    """kind: 'microphone' | 'desktop'."""
    if which("pw-record"):
        # --raw: plain PCM on stdout (otherwise pw-record writes a WAV container)
        cmd = ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1"]
        if kind == "desktop":
            # capture the default output's monitor
            cmd += ["-P", "stream.capture.sink=true"]
            if source and source != "@DEFAULT_MONITOR@":
                cmd += ["--target", source]
        elif source:
            cmd += ["--target", source]
        return cmd + ["-"]
    if which("parec"):
        cmd = ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30"]
        dev = source or ("@DEFAULT_MONITOR@" if kind == "desktop" else "")
        if dev:
            cmd.append(f"--device={dev}")
        return cmd
    if which("arecord") and kind == "microphone":
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]
    raise RuntimeError("no audio capture tool (pw-record, parec or arecord)")


class Capture:
    """A running capture process yielding 30 ms frames."""

    def __init__(self, source: str, kind: str) -> None:
        self.kind = kind
        self.cmd = capture_command(source, kind)
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL)

    def frames(self) -> Iterator[bytes]:
        assert self.proc.stdout is not None
        while True:
            data = self.proc.stdout.read(FRAME_BYTES)
            if not data or len(data) < FRAME_BYTES:
                return
            yield data

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------

def _player(raw: bool, rate: int, target: str) -> list[str]:
    if which("pw-play"):
        cmd = ["pw-play"]
        if raw:
            # --raw is required: without it pw-play parses stdin as a sound file and plays nothing
            cmd += ["--raw", "--format", "s16", "--rate", str(rate), "--channels", "1"]
        if target:
            cmd += ["--target", target]
        return cmd + ["-"]
    if which("paplay"):
        cmd = ["paplay"]
        if raw:
            cmd += ["--raw", "--format=s16le", f"--rate={rate}", "--channels=1"]
        if target:
            cmd.append(f"--device={target}")
        return cmd
    if which("aplay"):
        return ["aplay", "-q"] + (["-f", "S16_LE", "-r", str(rate), "-c", "1", "-t", "raw"] if raw else [])
    raise RuntimeError("no audio player (pw-play, paplay or aplay)")


class Playback:
    """Plays PCM to one or more targets; ``pause``/``resume``/``stop`` work while
    playing (clicking the 'responding' indicator toggles pause)."""

    def __init__(self, pcm: bytes, rate: int, targets: list[str]) -> None:
        self.pcm, self.rate, self.targets = pcm, rate, targets or [""]
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.done = threading.Event()
        self.error: str | None = None

    def play(self) -> None:
        procs = []
        try:
            procs = [subprocess.Popen(_player(True, self.rate, t), stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for t in self.targets]
            step = self.rate * 2 // 20   # 50 ms chunks so pause/stop react quickly
            for i in range(0, len(self.pcm), step):
                if self.stopped.is_set():
                    break
                while self.paused.is_set() and not self.stopped.is_set():
                    self.stopped.wait(0.05)
                chunk = self.pcm[i:i + step]
                for p in procs:
                    try:
                        assert p.stdin is not None
                        p.stdin.write(chunk)
                        p.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
            for p in procs:
                try:
                    assert p.stdin is not None
                    p.stdin.close()
                except OSError:
                    pass
            for p in procs:
                if self.stopped.is_set():
                    p.terminate()
                try:
                    p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    p.kill()
                    continue
                err = p.stderr.read() if p.stderr else b""
                if p.returncode not in (0, None) and not self.stopped.is_set():
                    self.error = (err or b"").decode(errors="replace").strip()[-300:] or f"exit {p.returncode}"
                    log.warning("audio player %s failed: %s", p.args[0], self.error)
        finally:
            self.done.set()

    def toggle_pause(self) -> bool:
        if self.paused.is_set():
            self.paused.clear()
        else:
            self.paused.set()
        return self.paused.is_set()

    def stop(self) -> None:
        self.stopped.set()
        self.paused.clear()


def play_file(path: Path, target: str = "") -> None:
    cmd = _player(False, RATE, target)
    if cmd[-1] == "-":
        cmd = cmd[:-1] + [str(path)]
    else:
        cmd = cmd + [str(path)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def theme_sound(name: str) -> Path | None:
    dirs = [Path(d) / "sounds" for d in (os.environ.get("XDG_DATA_DIRS", "/run/current-system/sw/share:/usr/share")
                                         .split(":"))] + [Path.home() / ".local/share/sounds"]
    for d in dirs:
        for theme in ("freedesktop", "ocean", "oxygen", ""):
            for ext in (".oga", ".ogg", ".wav"):
                p = d / theme / "stereo" / f"{name}{ext}" if theme else d / f"{name}{ext}"
                if p.is_file():
                    return p
    return None


def wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data)) as w:
        pcm = w.readframes(w.getnframes())
        rate = w.getframerate()
        if w.getnchannels() == 2:
            a = array.array("h")
            a.frombytes(pcm)
            pcm = array.array("h", a[0::2]).tobytes()
    return pcm, rate


def ensure_virtual_mic(sink: str) -> bool:
    """Create the null sink (and a source remapped from its monitor so apps list
    it as a microphone). Idempotent."""
    if not which("pactl"):
        return False
    try:
        sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=3).stdout
        if sink not in sinks:
            subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={sink}",
                            "sink_properties=device.description=Jeeves-Output"], capture_output=True, timeout=3)
            subprocess.run(["pactl", "load-module", "module-remap-source", f"master={sink}.monitor",
                            f"source_name={sink}-source", "source_properties=device.description=Jeeves-Microphone"],
                           capture_output=True, timeout=3)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def output_targets(output_to: str, speaker: str, virtual_sink: str) -> list[str]:
    targets = []
    if output_to in ("speakers", "both"):
        targets.append(speaker or "")
    if output_to in ("microphone", "both") and ensure_virtual_mic(virtual_sink):
        targets.append(virtual_sink)
    return targets or [speaker or ""]


def have_player() -> bool:
    return any(shutil.which(x) for x in ("pw-play", "paplay", "aplay"))


StopFn = Callable[[], bool]
