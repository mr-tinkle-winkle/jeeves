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
import queue
import shutil
import subprocess
import threading
import time
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
    """kind: 'microphone' | 'desktop' | 'device' (an exact source: a mic or an output's .monitor).

    Desktop audio is the default output's *monitor* (everything you hear: games,
    Discord voices, videos). parec addresses it reliably as @DEFAULT_MONITOR@ through
    PipeWire's Pulse layer; pw-record needs stream.capture.sink, used as a fallback."""
    if kind == "device":
        if not source:
            raise RuntimeError("no device chosen")
        if which("parec"):
            return ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30",
                    f"--device={source}"]
        if which("pw-record"):
            cmd = ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1"]
            if source.endswith(".monitor"):
                cmd += ["-P", "{ stream.capture.sink = true }", "--target", source.removesuffix(".monitor")]
            else:
                cmd += ["--target", source]
            return cmd + ["-"]
        raise RuntimeError("no audio capture tool (parec or pw-record)")
    if kind == "desktop":
        dev = source or "@DEFAULT_MONITOR@"
        if which("parec"):
            return ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30",
                    f"--device={dev}"]
        if which("pw-record"):
            cmd = ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1",
                   "-P", "{ stream.capture.sink = true }"]
            if dev != "@DEFAULT_MONITOR@":
                cmd += ["--target", dev.removesuffix(".monitor")]
            return cmd + ["-"]
        raise RuntimeError("no tool to record desktop audio (parec or pw-record)")
    mic = source or real_mic_source() or ""
    if which("pw-record"):
        # --raw: plain PCM on stdout (otherwise pw-record writes a WAV container)
        cmd = ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE), "--channels", "1"]
        if mic:
            cmd += ["--target", mic]
        return cmd + ["-"]
    if which("parec"):
        cmd = ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=30"]
        if mic:
            cmd.append(f"--device={mic}")
        return cmd
    if which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"]
    raise RuntimeError("no audio capture tool (pw-record, parec or arecord)")


STALL_SECONDS = 3.0        # no audio at all for this long: the capture is stuck, restart it
FOLLOW_SECONDS = 5.0       # how often to check that "the default microphone" hasn't changed


class Capture:
    """A running capture process yielding 30 ms frames.

    Frames are read on a separate thread so a capture that stops delivering (the computer slept and
    woke, a USB/Bluetooth mic reconnected, PipeWire restarted) is noticed and ends instead of
    blocking forever -- the listener then starts a new one. With no microphone chosen in settings,
    the capture also ends when the default microphone changes, so it follows the new one."""

    def __init__(self, source: str, kind: str) -> None:
        self.kind = kind
        self.follow_default = kind == "microphone" and not source
        self.cmd = capture_command(source, kind)
        self.pinned = next((a for a in self.cmd if a.startswith("--device=")), "").removeprefix("--device=") or \
            (self.cmd[self.cmd.index("--target") + 1] if "--target" in self.cmd else "")
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL)
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=400)    # ~12 s
        threading.Thread(target=self._read, daemon=True, name="jeeves-capture").start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                data = self.proc.stdout.read(FRAME_BYTES)
                if not data or len(data) < FRAME_BYTES:
                    break
                try:
                    self._q.put(data, timeout=5)
                except queue.Full:          # nobody is taking frames any more
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    def backlog(self) -> int:
        """Frames read but not yet taken: more than zero means the listener is behind."""
        return self._q.qsize()

    def frames(self) -> Iterator[bytes]:
        checked = time.monotonic()
        while True:
            try:
                data = self._q.get(timeout=STALL_SECONDS)
            except queue.Empty:
                log.warning("%s capture delivered no audio for %.0fs; restarting it", self.kind, STALL_SECONDS)
                return
            if data is None:
                return
            yield data
            if self.follow_default and time.monotonic() - checked >= FOLLOW_SECONDS:
                checked = time.monotonic()
                now_default = real_mic_source() or ""
                if now_default and self.pinned and now_default != self.pinned:
                    log.info("default microphone changed (%s -> %s); switching", self.pinned, now_default)
                    return

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
            try:
                procs = [subprocess.Popen(_player(True, self.rate, t), stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for t in self.targets]
            except (RuntimeError, OSError) as exc:     # no player installed, or it can't start: say why
                self.error = str(exc)
                log.warning("can't play audio: %s", exc)
                for p in procs:
                    p.kill()
                return
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


VIRTUAL_MIC_SOURCE = "{sink}-source"


def _pactl(*args: str) -> str:
    try:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def real_mic_source(virtual_sink: str = "jeeves-mic") -> str | None:
    """The default microphone -- unless the default is Jeeves' own virtual mic (then
    the first real one), so Jeeves never listens to itself."""
    if not which("pactl"):
        return None
    default = _pactl("get-default-source").strip()
    if default and not default.startswith(virtual_sink) and not default.endswith(".monitor"):
        return default
    for line in _pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and not parts[1].endswith(".monitor") and not parts[1].startswith(virtual_sink):
            return parts[1]
    return None


def ensure_virtual_mic(sink: str, include_mic: bool = True, mic: str = "") -> bool:
    """A microphone other apps (Discord, OBS) can pick that carries Jeeves' voice *and*
    yours:

        your mic --loopback--> [jeeves-mic null sink] <-- Jeeves speaks here
                                      |
                       monitor, remapped as the source "Jeeves Microphone"

    Idempotent: modules already loaded (matched by their arguments) are left alone."""
    if not which("pactl"):
        return False
    source_name = VIRTUAL_MIC_SOURCE.format(sink=sink)
    default_before = _pactl("get-default-source").strip()
    modules = _pactl("list", "short", "modules")
    if f"sink_name={sink}" not in modules:
        _pactl("load-module", "module-null-sink", f"sink_name={sink}",
               "sink_properties=device.description=Jeeves-Voice")
    if f"source_name={source_name}" not in modules:
        _pactl("load-module", "module-remap-source", f"master={sink}.monitor", f"source_name={source_name}",
               "source_properties=device.description=Jeeves-Microphone")
    loop_tag = f"sink={sink} "
    has_loop = any("module-loopback" in ln and loop_tag in ln + " " for ln in modules.splitlines())
    if include_mic and not has_loop:
        real = mic or real_mic_source(sink)
        if real:
            _pactl("load-module", "module-loopback", f"source={real}", f"sink={sink}", "latency_msec=20",
                   "source_dont_move=true", "sink_dont_move=true")
    elif not include_mic and has_loop:
        for ln in modules.splitlines():
            if "module-loopback" in ln and loop_tag in ln + " ":
                _pactl("unload-module", ln.split("\t")[0])
    # never let the desktop switch your default microphone to Jeeves' virtual one
    if default_before and not default_before.startswith(sink) and \
            _pactl("get-default-source").strip().startswith(sink):
        _pactl("set-default-source", default_before)
    return True


def output_targets(output_to: str, speaker: str, virtual_sink: str, include_mic: bool = True,
                   mic: str = "", device: str = "") -> list[str]:
    """Where an agent's voice plays. output_to: speakers | microphone | both | device (one exact
    output, e.g. a headset) | device_mic (that output and Jeeves-Microphone)."""
    targets = []
    if output_to in ("speakers", "both") or (output_to in ("device", "device_mic") and not device):
        targets.append(speaker or "")
    if output_to in ("device", "device_mic") and device:
        targets.append(device)
    if output_to in ("microphone", "both", "device_mic") and ensure_virtual_mic(virtual_sink, include_mic, mic):
        targets.append(virtual_sink)
    return targets or [speaker or ""]


def list_outputs() -> list[dict[str, str]]:
    """Every output (sink) an agent could speak through, with readable names."""
    import json as _json
    if not which("pactl"):
        return []
    try:
        items = _json.loads(_pactl("-f", "json", "list", "sinks") or "[]")
    except ValueError:
        items = []
    if items:
        return [{"name": it["name"], "description": it.get("description") or it["name"]}
                for it in items if it.get("name") and not it["name"].startswith("jeeves-mic")]
    out = []
    for line in _pactl("list", "short", "sinks").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and not parts[1].startswith("jeeves-mic"):
            out.append({"name": parts[1], "description": parts[1]})
    return out


def have_player() -> bool:
    return any(shutil.which(x) for x in ("pw-play", "paplay", "aplay"))


StopFn = Callable[[], bool]


def list_devices() -> list[dict[str, str]]:
    """Every source you could listen to: microphones and each output's monitor
    (what that output plays), with readable names."""
    import json as _json
    out: list[dict[str, str]] = []
    if not which("pactl"):
        return out
    raw = _pactl("-f", "json", "list", "sources")
    try:
        items = _json.loads(raw) if raw.strip() else []
    except ValueError:
        items = []
    if items:
        for it in items:
            name = it.get("name", "")
            if not name or name.startswith("jeeves-mic"):
                continue
            out.append({"name": name, "description": it.get("description") or name,
                        "kind": "output" if name.endswith(".monitor") else "microphone"})
        return out
    for line in _pactl("list", "short", "sources").splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and not parts[1].startswith("jeeves-mic"):
            out.append({"name": parts[1], "description": parts[1],
                        "kind": "output" if parts[1].endswith(".monitor") else "microphone"})
    return out
