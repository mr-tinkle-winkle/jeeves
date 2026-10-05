"""``jeeves doctor``: checks that Screen Reading and Control Mode can work on this
desktop, and says what to fix when they can't. Runs inside the daemon so it sees
exactly what the daemon sees (environment, permissions, tools)."""
from __future__ import annotations

import os
import time
from typing import Any

from ..util import desktop, which
from . import desktop as dk


def _check(name: str, ok: bool, detail: str, fix: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


def audio_checks(settings: Any) -> list[dict[str, Any]]:
    from .audio import VIRTUAL_MIC_SOURCE, _pactl, real_mic_source
    out: list[dict[str, Any]] = []
    if not which("pactl"):
        return [_check("Audio", False, "pactl isn't installed", "install pulseaudio's tools (pactl)")]
    sink = settings.get("audio.virtual_mic_sink", "jeeves-mic") if settings is not None else "jeeves-mic"
    source = VIRTUAL_MIC_SOURCE.format(sink=sink)
    sources = [ln.split("\t")[1] for ln in _pactl("list", "short", "sources").splitlines() if "\t" in ln]
    default = _pactl("get-default-source").strip()
    real = real_mic_source(sink)
    out.append(_check("Microphone", bool(real), f"Jeeves listens to: {real or 'nothing'} (system default: "
                      f"{default or 'none'})", "" if real else "no microphone found"))
    has = source in sources
    loops = [ln for ln in _pactl("list", "short", "modules").splitlines()
             if "module-loopback" in ln and f"sink={sink} " in ln + " "]
    detail = f"'Jeeves-Microphone' ({source}) " + ("exists" if has else "is missing")
    if loops:
        detail += "; carries " + loops[0].split("source=")[1].split()[0]
    out.append(_check("Jeeves-Microphone", has, detail,
                      "" if has else "it's created when Jeeves is on; check that Jeeves is on"))
    agents = (settings.get("agents", {}) or {}) if settings is not None else {}
    into = [a.get("name", k) for k, a in agents.items() if a.get("output_to") in ("microphone", "both")]
    both = [a.get("name", k) for k, a in agents.items() if a.get("output_to") == "both"]
    out.append(_check("Agents speaking into it", bool(into), ", ".join(into) if into else "none",
                      "" if into else "set an agent's Speaks through to Microphone or Both (Agents page), or "
                      "friends only hear you"))
    if both:
        out.append(_check("Echo cancellation", True, f"{', '.join(both)} speak(s) through speakers and the mic",
                          "if friends can't hear the agent, turn off Echo Cancellation in Discord's Voice "
                          "settings (it removes sound that also comes out of your speakers), or use Microphone "
                          "instead of Both"))
    return out


def function_checks(settings: Any, registry: Any) -> list[dict[str, Any]]:
    out = []
    agents = (settings.get("agents", {}) or {}) if settings is not None else {}
    for aid, a in agents.items():
        if a.get("deleted"):
            continue
        on = {f.name for f in registry.enabled_for(a)}
        for fname, title in (("screen_reading", "Screen Reading"), ("control_mode", "Control Mode")):
            ok = fname in on
            out.append(_check(f"{title} for {a.get('name', aid)}", ok, "on" if ok else "off",
                              "" if ok else f"turn it on in Agents > {a.get('name', aid)} > Functions"))
    return out


def run(control: Any = None, move_test: bool = True, settings: Any = None,
        registry: Any = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if settings is not None and registry is not None:
        out += function_checks(settings, registry)
    try:
        from ..models import hardware
        hw = hardware.detect()
        def gpu_desc(g: dict[str, Any]) -> str:
            mem = "VRAM unknown" if g.get("vram_unknown") else "%.0f GB" % (g["vram_mb"] / 1024)
            return f"{g['name']} ({mem})"
        gpus = ", ".join(gpu_desc(g) for g in hw["gpus"]) or "none found"
        backend = ", ".join(hw["llama_gpu"]) or "CPU-only build"
        ok = bool(hw["gpus"]) and bool(hw["llama_gpu"]) and not any(g.get("vram_unknown") for g in hw["gpus"])
        out.append(_check("GPU", ok, f"{gpus}; llama.cpp: {backend}",
                          "" if ok else "no GPU found: for NVIDIA the driver must be loaded" if not hw["gpus"] else
                          "set services.jeeves.acceleration (\"cuda\" or \"vulkan\") and rebuild"
                          if not hw["llama_gpu"] else "the NVIDIA driver's NVML library / nvidia-smi wasn't found"))
    except Exception as exc:
        out.append(_check("GPU", False, str(exc)))
    if settings is not None:
        try:
            out += audio_checks(settings)
        except Exception as exc:
            out.append(_check("Audio", False, str(exc)))
    d = desktop()
    disp = os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY") or ""
    out.append(_check("Desktop", bool(disp),
                      f"{d} (display: {disp or 'none'}, XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '')})",
                      "" if disp else "the daemon can't see the desktop session; run "
                      "'systemctl --user import-environment WAYLAND_DISPLAY XDG_CURRENT_DESKTOP' from the desktop, "
                      "or restart the service after logging in"))

    box = None
    known = False
    try:
        outs = dk.outputs()
        box = dk.desktop_box()
        desc = ", ".join(f"{o.name} {o.w}x{o.h}+{o.x}+{o.y}@{o.scale:g}{' (primary)' if o.primary else ''}"
                         for o in outs)
        known = not (len(outs) == 1 and outs[0].name == "default")
        out.append(_check("Monitors", known, f"{desc}; desktop {box[2]}x{box[3]} at {box[0]},{box[1]}",
                          "" if known else "couldn't read the monitor layout (kscreen-doctor or hyprctl); "
                          "clicks may land in the wrong place on multi-monitor or scaled setups"))
    except Exception as exc:
        out.append(_check("Monitors", False, str(exc)))

    from ..functions.partials import screen
    shot = None
    try:
        shot = screen.screenshot()
        iw, ih = screen._image_size(shot)
        mp = screen.Mapper((iw, ih), box if known else None)
        out.append(_check("Screenshot", True, f"{screen.last_tool}: {iw}x{ih} px "
                          f"(1 px = {mp.sx:.2f} x {mp.sy:.2f} desktop units)"))
    except Exception as exc:
        out.append(_check("Screenshot", False, str(exc),
                          "on KDE install spectacle (or allow Jeeves when the desktop asks to share the "
                          "screen); on Hyprland/sway install grim"))
    if shot is not None:
        try:
            words = screen.ocr_words(shot)
            out.append(_check("Reading text (OCR)", bool(words), f"tesseract read {len(words)} words",
                              "" if words else "nothing readable on screen right now, or the screenshot was black"))
        except Exception as exc:
            out.append(_check("Reading text (OCR)", False, str(exc), "install tesseract"))
        finally:
            shot.unlink(missing_ok=True)

    pos = None
    try:
        pos = dk.mouse_position()
        out.append(_check("Mouse position", True, f"{pos[0]},{pos[1]}"))
    except Exception as exc:
        out.append(_check("Mouse position", False, str(exc),
                          "install kdotool (KDE) -- without it moves are relative and less precise"))
    try:
        f = dk.focused()
        out.append(_check("Window queries", True, f"focused: {f.app} -- {f.title}" if f else "no focused window"))
    except Exception as exc:
        out.append(_check("Window queries", False, str(exc)))

    if control is None:
        return out
    if not control.available():
        out.append(_check("Virtual input devices", False, "python-evdev isn't installed"))
        return out
    try:
        with control._lock:
            control._ensure()
        out.append(_check("Virtual input devices", True, "jeeves-keyboard, jeeves-mouse, jeeves-mouse-absolute"))
    except Exception as exc:
        out.append(_check("Virtual input devices", False, str(exc),
                          "enable services.jeeves (adds the uinput rule and the input group), then log out and in"))
        return out
    if move_test and pos is not None and box is not None:
        tx, ty = box[0] + box[2] // 3, box[1] + box[3] // 3
        try:
            control.move(tx, ty, absolute=True)
            time.sleep(0.25)
            got = dk.mouse_position()
            err = abs(got[0] - tx) + abs(got[1] - ty)
            out.append(_check("Exact mouse moves", err <= 4,
                              f"asked for {tx},{ty}, cursor went to {got[0]},{got[1]}",
                              "" if err <= 4 else "the absolute pointer isn't mapped to the whole desktop; "
                              "please report this output"))
        except Exception as exc:
            out.append(_check("Exact mouse moves", False, str(exc)))
        finally:
            try:
                control.move(pos[0], pos[1], absolute=True)
            except Exception:
                pass
    elif move_test:
        out.append(_check("Exact mouse moves", False, "skipped: the mouse position can't be read",
                          "install kdotool (KDE)" if d == "kde" else ""))
    return out


def format_report(checks: list[dict[str, Any]]) -> str:
    lines = []
    for c in checks:
        lines.append(f"[{'ok' if c['ok'] else '!!'}] {c['name']}: {c['detail']}")
        if not c["ok"] and c.get("fix"):
            lines.append(f"     -> {c['fix']}")
    return "\n".join(lines)


__all__ = ["run", "format_report", "which"]
