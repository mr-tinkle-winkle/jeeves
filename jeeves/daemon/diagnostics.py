"""Everything about how Jeeves runs on this computer, in one text to copy (Activity > Logs).

Made for "why is it slow / broken here and not there": the build, the computer (CPU, its speed and
temperature, memory, the GPU and its free memory, the driver), what each model runs on and how
(the exact model-server command lines, how many layers went to the GPU, what the servers said while
loading), how fast they really ran (tokens per second, seconds of speech recognised per second),
how long each recent request took and where the time went, how listening is set up, the settings
that matter for speed, what Jeeves itself is using, and the warnings and errors since it started.

Written to ~/.local/state/jeeves/diagnostics.txt a minute after each daemon start (and whenever the
page asks), so it's there to copy even when the GUI isn't. Nothing private: no API keys or tokens, no
transcripts of what was heard (only what was asked of it, as on the Activity page), no file contents.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import paths

LOG_LINES: collections.deque[str] = collections.deque(maxlen=400)
STARTED = time.time()


class _Ring(logging.Handler):
    """The daemon's own log since it started (warnings and errors, plus the lines about models,
    listening and timing), kept for the report."""

    KEEP = re.compile(r"starting|GPU layers|loaded|unloaded|listening with|crash|failed|resting|speech model|"
                      r"moves to|listens to|wake model|build|stopping|switched", re.I)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if record.levelno >= logging.WARNING or self.KEEP.search(msg):
            LOG_LINES.append(time.strftime("%H:%M:%S", time.localtime(record.created)) +
                             f" {record.levelname[0]} {record.name.replace('jeeves.', '')}: {msg[:600]}")


def install() -> None:
    logging.getLogger().addHandler(_Ring(level=logging.INFO))


def _run(argv: list[str], timeout: float = 4.0) -> str:
    if not shutil.which(argv[0]) and not os.path.exists(argv[0]):
        return ""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------- the computer
def cpu() -> list[str]:
    info = _read("/proc/cpuinfo")
    model = next((ln.split(":", 1)[1].strip() for ln in info.splitlines() if ln.startswith("model name")), "?")
    mhz = [float(ln.split(":", 1)[1]) for ln in info.splitlines() if ln.startswith("cpu MHz")]
    out = [f"CPU: {model}; {os.cpu_count()} threads"
           + (f"; now {min(mhz):.0f}-{max(mhz):.0f} MHz" if mhz else "")]
    gov = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    epp = _read("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
    driver = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")
    mx = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    if gov or driver:
        out.append(f"  frequency: driver {driver or '?'}, governor {gov or '?'}"
                   + (f", energy preference {epp}" if epp else "") + (f", max {int(mx) // 1000} MHz" if mx else ""))
    prof = _read("/sys/firmware/acpi/platform_profile")
    if prof:
        out.append(f"  platform profile: {prof}")
    load = os.getloadavg() if hasattr(os, "getloadavg") else None
    if load:
        out.append(f"  load average: {load[0]:.1f} {load[1]:.1f} {load[2]:.1f} (1/5/15 min)")
    temps = []
    for z in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        t, kind = _read(str(z / "temp")), _read(str(z / "type"))
        if t.lstrip("-").isdigit() and kind:
            temps.append(f"{kind} {int(t) / 1000:.0f}°C")
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        name = _read(str(h / "name"))
        if name in ("k10temp", "coretemp", "zenpower", "amdgpu", "nvme"):
            for f in sorted(h.glob("temp*_input"))[:3]:
                lab = _read(str(f).replace("_input", "_label")) or f.name
                v = _read(str(f))
                if v.isdigit():
                    temps.append(f"{name}/{lab} {int(v) / 1000:.0f}°C")
    if temps:
        out.append("  temperatures: " + ", ".join(temps[:10]))
    return out


def memory() -> list[str]:
    mi = {}
    for ln in _read("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        if v.strip().split():
            mi[k] = int(v.split()[0]) // 1024
    swap = f"; swap {mi.get('SwapTotal', 0) - mi.get('SwapFree', 0)}/{mi.get('SwapTotal', 0)} MB used" \
        if mi.get("SwapTotal") else "; no swap"
    return [f"RAM: {mi.get('MemTotal', 0)} MB, {mi.get('MemAvailable', 0)} MB available{swap}"]


def gpu(hw: dict[str, Any]) -> list[str]:
    out = []
    smi = next((p for p in ("nvidia-smi", "/run/current-system/sw/bin/nvidia-smi", "/run/opengl-driver/bin/nvidia-smi")
                if shutil.which(p) or os.path.exists(p)), None)
    if smi:
        q = _run([smi, "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu,"
                  "power.draw,pstate,pcie.link.gen.current,pcie.link.width.current", "--format=csv,noheader"])
        for ln in q.splitlines():
            out.append(f"GPU (nvidia-smi): {ln}")
        procs = _run([smi, "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"])
        if procs:
            out.append("  using the GPU: " + "; ".join(p.strip() for p in procs.splitlines()))
    rocm = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--showuse"]) if shutil.which("rocm-smi") else ""
    if rocm:
        out += ["GPU (rocm-smi): " + ln for ln in rocm.splitlines() if ln.strip() and not ln.startswith("=")][:12]
    for card in sorted(Path("/sys/class/drm").glob("card?")):
        dev = card / "device"
        vendor = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}.get(_read(str(dev / "vendor")), "")
        if not vendor:
            continue
        tot, used = _read(str(dev / "mem_info_vram_total")), _read(str(dev / "mem_info_vram_used"))
        busy = _read(str(dev / "gpu_busy_percent"))
        extra = (f", VRAM {int(used) // 2**20}/{int(tot) // 2**20} MB" if tot.isdigit() and used.isdigit() else "") + \
                (f", busy {busy}%" if busy else "")
        out.append(f"GPU device {card.name}: {vendor} {_read(str(dev / 'device'))}{extra}")
    lspci = _run(["lspci"])
    out += ["  lspci: " + ln for ln in lspci.splitlines() if re.search(r"VGA|3D|Display", ln)][:4]
    out.append(f"Jeeves sees: {json.dumps(hw.get('gpus'))}; VRAM {hw.get('vram_mb')} MB; llama.cpp GPU backends "
               f"{hw.get('llama_gpu')}; whisper.cpp GPU backends {hw.get('whisper_gpu')}; usable: {hw.get('gpu_usable')}")
    return out


# ---------------------------------------------------------------------------- Jeeves
def _proc_line(pid: int) -> str:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu_s = (int(stat[11]) + int(stat[12])) / ticks
        age = time.time() - (_boot() + int(stat[19]) / ticks)
        rss = int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") // 2**20
        threads = stat[17]
        return f"pid {pid}: {rss} MB RAM, {threads} threads, {cpu_s:.0f} s of CPU in {age:.0f} s ({cpu_s / max(age, 1) * 100:.0f}% of a core on average)"
    except (OSError, IndexError, ValueError):
        return f"pid {pid}: gone"


def _boot() -> float:
    for ln in _read("/proc/stat").splitlines():
        if ln.startswith("btime"):
            return float(ln.split()[1])
    return 0.0


def models(engine: Any) -> list[str]:
    m = engine.models
    out = []
    try:
        st = m.status()
    except Exception as exc:  # noqa: BLE001
        return [f"(model status failed: {exc})"]
    for kind, k in st.get("kinds", {}).items():
        out.append(f"{kind}: {k.get('model')} (chosen: {k.get('chosen')}; installed {k.get('installed')}; "
                   f"loaded {k.get('loaded')}" + (f"; suspended by {k['suspended_by']}" if k.get("suspended_by") else "")
                   + ")")
    out.append(f"budget for the AIs: {json.dumps(m.budget())}")
    out.append(f"GPU offload decisions: {json.dumps(st.get('gpu_offload'))}; hybrid: {json.dumps(st.get('hybrid'))}")
    with m._lock:
        insts = dict(m.instances)
    for mid, inst in insts.items():
        srv = getattr(inst, "server", None)
        out.append(f"\n[{mid}] loaded: {inst.loaded() if hasattr(inst, 'loaded') else '?'}")
        if srv is None:
            continue
        if srv.cmd:
            out.append("  command: " + " ".join(srv.cmd))
        if srv.load_seconds:
            out.append(f"  took {srv.load_seconds:.1f} s to load")
        if srv.proc is not None:
            out.append("  " + _proc_line(srv.proc.pid))
        useful = [ln for ln in srv.head if re.search(
            r"gpu|cuda|vulkan|rocm|hip|metal|offload|layer|vram|buffer|backend|device|threads|system_info|"
            r"n_ctx|flash|avx|model type|model size|ftype|error|warn|fail", ln, re.I) and "-->" not in ln]
        if useful:
            out.append("  while loading it said:")
            out += ["    " + ln[:300] for ln in useful[:45]]
        timing = [ln for ln in srv.tail if re.search(r"eval time|tokens per second|t/s|slot|timings", ln, re.I)
                  and "-->" not in ln]
        if timing:
            out.append("  lately:")
            out += ["    " + ln[:300] for ln in timing[-10:]]
    return out


def speed() -> list[str]:
    from ..models.backends import STATS
    out = []
    llm = list(STATS["llm"])
    if llm:
        out.append("Language model calls (newest last):")
        for s in llm[-20:]:
            out.append(f"  {time.strftime('%H:%M:%S', time.localtime(s['at']))} {s['model']}: {s['took_s']} s"
                       f"{' thinking' if s['think'] else ''}{' streamed' if s['stream'] else ''}; prompt "
                       f"{s['prompt_tokens'] or '?'} tok ({s['prompt_chars']} chars) read at {s['read_tok_s'] or '?'} tok/s;"
                       f" wrote {s['made_tokens'] or '?'} tok at {s['write_tok_s'] or '?'} tok/s "
                       f"(answer {s['answer_chars']} chars, thinking {s['thought_chars']} chars)")
        w = [s["write_tok_s"] for s in llm if s.get("write_tok_s")]
        if w:
            out.append(f"  writing speed: median {sorted(w)[len(w) // 2]} tok/s (a GPU usually gives 30-150+, a CPU 2-15)")
    else:
        out.append("Language model calls: none yet since the daemon started")
    stt = list(STATS["stt"])
    if stt:
        out.append("Speech recognition (newest last):")
        for s in stt[-20:]:
            out.append(f"  {time.strftime('%H:%M:%S', time.localtime(s['at']))} {s['model']}: {s['audio_s']} s of audio "
                       f"in {s['took_s']} s ({s['speed']}x realtime){'' if s['ok'] else ' FAILED'}"
                       f"{'; ' + str(s['busy_at_once']) + ' at once' if s['busy_at_once'] > 1 else ''}")
        n = len(stt)
        out.append(f"  {n} in the last while; {sum(s['took_s'] for s in stt):.0f} s spent recognising "
                   f"{sum(s['audio_s'] for s in stt):.0f} s of audio")
    else:
        out.append("Speech recognition: none yet since the daemon started")
    return out


def requests(engine: Any, n: int = 15) -> list[str]:
    out = []
    try:
        items = engine.history.list(0, n, None)
    except Exception:  # noqa: BLE001
        return ["(history unavailable)"]
    for e in reversed(items):
        tr = e.get("trace") or []
        total = max((t.get("t", 0) for t in tr), default=0)
        marks = []
        last = 0.0
        for t in tr:
            k = t.get("kind")
            if k in ("intent", "call", "say", "result", "sources", "planned", "speech", "aborted", "error"):
                gap = t.get("t", 0) - last
                marks.append(f"{k}@{t.get('t', 0):.1f}" + (f"(+{gap:.1f})" if gap >= 2 else ""))
                last = t.get("t", 0)
        intent = next((t for t in tr if t.get("kind") == "intent"), {})
        out.append(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(e.get('time', 0)))} {e.get('agent')} "
                   f"[{e.get('source')}] \"{(e.get('text') or '')[:80]}\" -> {e.get('function')} "
                   f"({intent.get('method', '?')}, confidence {intent.get('confidence', '?')}) {e.get('status')}; "
                   f"{total:.1f} s total")
        out.append("    " + " ".join(marks[:30]))
    return out or ["(no requests yet)"]


SPEED_SETTINGS = ["models", "wake_word", "summary", "audio", "general", "indicators", "control_mode", "persona"]
SECRET = re.compile(r"(?:^|_)(?:api_key|token|password|secret|cookies?|pw)(?:$|_)", re.I)


def _redact(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: ("<hidden>" if SECRET.search(k) and v else _redact(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_redact(v) for v in x]
    return x


def settings(engine: Any) -> list[str]:
    s = engine.settings
    out = []
    for key in SPEED_SETTINGS:
        out.append(f"{key}: {json.dumps(_redact(s.get(key, {})), default=str)}")
    for aid, a in (s.get("agents", {}) or {}).items():
        fns = a.get("functions") or {}
        out.append(f"agent {aid} ({a.get('name')}): enabled {a.get('enabled')}; names {a.get('call_names')}; listens to "
                   f"{a.get('listen_to')} {a.get('listen_device') or ''}; jeenius {a.get('jeenius')}; threshold "
                   f"{a.get('threshold')}; models {json.dumps(a.get('models'))}; rudimentary "
                   f"{(a.get('rudimentary') or {}).get('function') if (a.get('rudimentary') or {}).get('enabled') else 'no'}; "
                   f"functions on: {', '.join(k for k, v in fns.items() if v)}; every line in character "
                   f"{a.get('persona_every_line')}; mute {a.get('mute')}")
    return out


def listening(engine: Any) -> list[str]:
    out = []
    for src, lst in list(engine.listeners.items()):
        out.append(f"{src}: {json.dumps(getattr(lst, 'status', {}), default=str)[:400]}; "
                   f"{engine._why_modes.get(src, '')}")
    try:
        from .audio import real_mic_source
        out.append(f"default microphone: {real_mic_source()}")
    except Exception:  # noqa: BLE001
        pass
    pa = _run(["pactl", "info"])
    out += ["  " + ln for ln in pa.splitlines() if re.search(r"Server Name|Default S|Server Version", ln)]
    return out or ["(not listening)"]


def report(engine: Any) -> str:
    from .. import __build__, __version__
    hw = engine.models.hardware() if hasattr(engine.models, "hardware") else {}
    me = os.getpid()
    sections: list[tuple[str, list[str]]] = [
        ("Jeeves", [f"build {__build__} (version {__version__}); daemon up {time.time() - STARTED:.0f} s; "
                    f"Python {sys.version.split()[0]}; {platform.platform()}",
                    f"session: {os.environ.get('XDG_CURRENT_DESKTOP', '?')} on {os.environ.get('XDG_SESSION_TYPE', '?')}",
                    "daemon " + _proc_line(me)]),
        ("Computer", cpu() + memory()),
        ("GPU", gpu(hw)),
        ("Models", models(engine)),
        ("How fast they ran", speed() + ([f"Speed test ({time.strftime('%H:%M:%S', time.localtime(SPEED['at']))}):"]
                                          + ["  " + ln for ln in SPEED["result"].splitlines()] if SPEED["result"]
                                          else ["Speed test: not run (Activity > Logs > Run a speed test)"])),
        ("Recent requests (where the time went)", requests(engine)),
        ("Listening", listening(engine)),
        ("Settings that matter for speed", settings(engine)),
        ("Log since it started (warnings, errors, models, listening)", list(LOG_LINES)[-150:]),
    ]
    head = f"Jeeves diagnostics — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
    return head + "\n".join(f"\n===== {title} =====\n" + "\n".join(lines) for title, lines in sections) + "\n"


SPEED: dict[str, Any] = {"running": False, "result": "", "at": 0.0}


def start_speed_test(engine: Any) -> dict[str, Any]:
    """Runs in the background (it can take minutes, and the GUI waits on nothing)."""
    import threading
    if not SPEED["running"]:
        SPEED["running"] = True

        def go() -> None:
            try:
                SPEED["result"] = speed_test(engine)
            finally:
                SPEED["running"], SPEED["at"] = False, time.time()
        threading.Thread(target=go, daemon=True, name="jeeves-speed-test").start()
    return dict(SPEED)


def speed_test(engine: Any) -> str:
    """The same small job on each model, timed: what this computer can do (the numbers to compare)."""
    import math
    import struct
    out = []
    m = engine.models
    for kind in ("intent", "local_response"):
        try:
            llm = m.llm(kind)
        except Exception as exc:  # noqa: BLE001
            out.append(f"{kind}: not available ({exc})")
            continue
        t0 = time.time()
        try:
            llm.chat([{"role": "user", "content": "Count from 1 to 40, numbers only, separated by spaces."}],
                     max_tokens=160, temperature=0.0, think=False)
            out.append(f"{kind} ({llm.model_name}): {time.time() - t0:.1f} s for ~120 tokens without thinking")
        except Exception as exc:  # noqa: BLE001
            out.append(f"{kind}: failed ({exc})")
    try:
        stt = m.stt()
        tone = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / 16000))) for i in range(16000 * 5))
        t0 = time.time()
        stt.transcribe(tone)
        out.append(f"speech recognition ({getattr(getattr(stt, 'entry', None), 'id', '?')}): "
                   f"{time.time() - t0:.1f} s for 5 s of audio")
    except Exception as exc:  # noqa: BLE001
        out.append(f"speech recognition: not available ({exc})")
    LOG_LINES.append(time.strftime("%H:%M:%S") + " I speed test: " + "; ".join(out))
    return "\n".join(out)


def write(engine: Any) -> str:
    text = report(engine)
    try:
        p = paths.state_dir() / "diagnostics.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    except OSError:
        pass
    return text
