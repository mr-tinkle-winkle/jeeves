"""Per-agent voice style: pitch and effects applied after synthesis (speed and
expressiveness are passed to the engines themselves).

Effects use SoX on raw 16-bit mono PCM, so the voice keeps its sample rate. Without
SoX the speech is returned unchanged (and the Agents page says SoX is missing).
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

EFFECTS: dict[str, tuple[str, list[str]]] = {
    "none": ("None", []),
    "radio": ("Radio", ["highpass", "300", "lowpass", "3400", "overdrive", "4", "gain", "-3"]),
    "telephone": ("Telephone", ["highpass", "500", "lowpass", "2800", "compand", "0.02,0.2", "-60,-40,-10", "-5"]),
    "robot": ("Robot", ["overdrive", "6", "flanger", "0", "3", "0", "90", "0.6", "50", "sin",
                        "echo", "0.8", "0.88", "6", "0.4", "gain", "-4"]),
    "hall": ("Big hall", ["reverb", "70", "50", "100"]),
    "room": ("Small room", ["reverb", "25", "50", "30"]),
    "cave": ("Cave echo", ["echo", "0.8", "0.85", "180", "0.35", "360", "0.2"]),
    "megaphone": ("Megaphone", ["highpass", "800", "lowpass", "4000", "overdrive", "12", "gain", "-6"]),
    "whisper": ("Breathy", ["highpass", "1200", "tremolo", "30", "20", "gain", "4"]),
    "chorus": ("Double voice", ["chorus", "0.6", "0.9", "50", "0.4", "0.25", "2", "-t"]),
    "underwater": ("Underwater", ["lowpass", "500", "tremolo", "6", "40", "reverb", "40"]),
    "alien": ("Alien", ["pitch", "300", "flanger", "0", "5", "0", "80", "2", "30", "tri", "echo", "0.8", "0.9", "40", "0.3"]),
}

DEFAULT_STYLE: dict[str, Any] = {
    "speaker": "",          # multi-speaker voices: speaker name or number ("" = the voice's own/first)
    "speed": 1.0,           # 0.5 .. 2.0
    "pitch": 0.0,           # semitones, -12 .. +12
    "expressiveness": 0.667,  # Piper noise scale: 0 = flat, 1 = lively
    "effect": "none",
    "blend": "",            # Kokoro: a second voice mixed in
    "blend_amount": 0.3,
}


def style_of(agent: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_STYLE)
    out.update({k: v for k, v in ((agent or {}).get("voice_style") or {}).items() if v is not None})
    try:
        out["speed"] = max(0.5, min(2.0, float(out["speed"])))
        out["pitch"] = max(-12.0, min(12.0, float(out["pitch"])))
        out["expressiveness"] = max(0.0, min(1.2, float(out["expressiveness"])))
        out["blend_amount"] = max(0.0, min(1.0, float(out["blend_amount"])))
    except (TypeError, ValueError):
        return dict(DEFAULT_STYLE)
    if out["effect"] not in EFFECTS:
        out["effect"] = "none"
    return out


def available() -> bool:
    return shutil.which("sox") is not None


def apply(pcm: bytes, rate: int, style: dict[str, Any], pitch_done: bool = False) -> bytes:
    """Pitch shift (unless the engine already did it) and the effect."""
    chain: list[str] = []
    if not pitch_done and abs(float(style.get("pitch", 0))) >= 0.05:
        chain += ["pitch", str(int(round(float(style["pitch"]) * 100)))]
    fx = EFFECTS.get(style.get("effect", "none"), EFFECTS["none"])[1]
    if fx:
        chain += fx + ["norm", "-3"]          # effects change loudness: bring it back to normal
    if not chain or not pcm or not available():
        return pcm
    raw = ["-t", "raw", "-r", str(rate), "-e", "signed", "-b", "16", "-c", "1"]
    try:
        out = subprocess.run(["sox", *raw, "-", *raw, "-", *chain], input=pcm, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return pcm
    return out.stdout if out.returncode == 0 and out.stdout else pcm
