"""What this computer can run, and which models to recommend for it.

Detection: total RAM (/proc/meminfo), CPU threads, GPUs with their VRAM
(nvidia-smi for NVIDIA, sysfs for AMD), and -- just as important -- whether
the installed llama.cpp / whisper.cpp were built with a GPU backend at all.
A big GPU doesn't help if llama.cpp is CPU-only (the NixOS module's
``services.jeeves.acceleration`` builds them with Vulkan, CUDA or ROCm).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import catalog

GPU_LIBS = {"vulkan": "libggml-vulkan", "cuda": "libggml-cuda", "rocm": "libggml-hip", "metal": "libggml-metal"}


def _ram_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 8192


def _gpus() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if shutil.which("nvidia-smi"):
        try:
            text = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5).stdout
            for line in text.splitlines():
                name, _, mem = line.rpartition(",")
                if mem.strip().isdigit():
                    out.append({"name": name.strip(), "vendor": "nvidia", "vram_mb": int(mem.strip())})
        except (OSError, subprocess.TimeoutExpired):
            pass
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        dev = card / "device"
        total = dev / "mem_info_vram_total"            # amdgpu
        if total.exists():
            try:
                vram = int(total.read_text().strip()) // (1024 * 1024)
            except (OSError, ValueError):
                continue
            if vram >= 1024:                           # skip APUs' tiny carve-outs
                name = "AMD GPU"
                try:
                    name = (dev / "product_name").read_text().strip() or name
                except OSError:
                    pass
                out.append({"name": name, "vendor": "amd", "vram_mb": vram})
    return out


def _backends(binary: str) -> list[str]:
    exe = shutil.which(binary)
    if not exe:
        return []
    real = Path(os.path.realpath(exe)).parent
    found = []
    for d in (real, real.parent / "lib"):
        if d.is_dir():
            names = [p.name for p in d.iterdir()]
            for backend, lib in GPU_LIBS.items():
                if any(n.startswith(lib) for n in names) and backend not in found:
                    found.append(backend)
    return found


def detect() -> dict[str, Any]:
    gpus = _gpus()
    llama = _backends("llama-server")
    whisper = _backends("whisper-server")
    vram = max((g["vram_mb"] for g in gpus), default=0)
    return {
        "ram_mb": _ram_mb(),
        "cpu_threads": os.cpu_count() or 4,
        "gpus": gpus,
        "vram_mb": vram,
        "llama_gpu": llama,           # GPU backends llama.cpp was built with
        "whisper_gpu": whisper,
        "gpu_usable": bool(vram and llama),
    }


# ---------------------------------------------------------------------------
# How each model fits
# ---------------------------------------------------------------------------

def fit(entry: catalog.ModelEntry, hw: dict[str, Any]) -> str:
    """'gpu' (fits in VRAM with a GPU backend), 'cpu' (runs from RAM), 'slow' (fits in
    RAM but too big to be responsive on a CPU) or 'no' (not enough memory)."""
    need = max(entry.ram_mb, 1)
    if entry.kind == "llm" and hw.get("gpu_usable") and entry.size_mb + 1000 <= hw["vram_mb"] * 0.95:
        return "gpu"
    if need > hw["ram_mb"] * 0.75:
        return "no"
    if entry.kind == "llm":
        active = entry.active_b or entry.params_b
        limit = 8 if hw["cpu_threads"] >= 12 else 4
        return "cpu" if active <= limit else "slow"
    if entry.kind == "stt" and entry.engine == "whisper.cpp" and not hw.get("whisper_gpu"):
        return "cpu" if entry.speed >= 3 or hw["cpu_threads"] >= 12 else "slow"
    return "cpu"


def _best(entries: list[catalog.ModelEntry], hw: dict[str, Any], min_speed: int = 1,
          fits: tuple[str, ...] = ("gpu", "cpu")) -> catalog.ModelEntry | None:
    ok = [e for e in entries if fit(e, hw) in fits and e.speed >= min_speed]
    if not ok:
        return None
    return max(ok, key=lambda e: (e.quality, e.speed, -e.ram_mb))


def recommend(hw: dict[str, Any] | None = None) -> dict[str, Any]:
    """{role: {"id", "why"}} for this computer, plus fast/smart alternatives."""
    hw = hw or detect()
    llms = catalog.of_kind("llm")
    where = "your GPU" if hw.get("gpu_usable") else "your CPU"
    out: dict[str, Any] = {}

    def pick(role: str, entry: catalog.ModelEntry | None, why: str) -> None:
        if entry is not None:
            out[role] = {"id": entry.id, "name": entry.name, "why": why}

    pick("wake", catalog.get("vosk-small-en"), "Light enough to listen all the time.")
    stts = catalog.of_kind("stt")
    if hw.get("whisper_gpu") and hw["vram_mb"] >= 3000:
        pick("stt", catalog.get("whisper-large-v3-turbo"), "Most accurate; whisper.cpp can use your GPU.")
    else:
        pick("stt", _best([e for e in stts if e.engine == "whisper.cpp"], hw, min_speed=3),
             f"The most accurate Whisper that still transcribes quickly on {where}.")
    pick("intent", _best(llms, hw, min_speed=4),
         "Picking a function needs speed more than knowledge: the best of the fast models.")
    pick("local_response", _best(llms, hw, min_speed=2),
         f"The smartest model that still answers at a comfortable speed on {where}.")
    pick("local_response_fast", _best(llms, hw, min_speed=4), "If you'd rather have instant answers.")
    pick("local_response_smart", _best(llms, hw, fits=("gpu", "cpu", "slow")),
         "The smartest model your memory can hold -- expect slow answers.")
    pick("tts", catalog.get("piper"), "Natural voices that run fast on any CPU.")
    pick("tts_voice", catalog.get("piper-en_GB-alan-medium"), "A suitably butler-ish voice (try the others too).")
    return {"hardware": hw, "picks": out}


# ---------------------------------------------------------------------------
# Auto GPU layers
# ---------------------------------------------------------------------------

def free_vram_mb() -> int | None:
    """Free memory on the biggest GPU right now (what other apps, the desktop and
    already-loaded models leave), or None if it can't be read."""
    best: int | None = None
    if shutil.which("nvidia-smi"):
        try:
            text = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5).stdout
            vals = [int(v) for v in text.split() if v.strip().isdigit()]
            if vals:
                best = max(vals)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        dev = card / "device"
        try:
            total = int((dev / "mem_info_vram_total").read_text()) // (1024 * 1024)
            used = int((dev / "mem_info_vram_used").read_text()) // (1024 * 1024)
        except (OSError, ValueError):
            continue
        if total >= 1024:
            best = max(best or 0, total - used)
    return best


def auto_gpu_layers(model_path: str | Path, ctx_size: int, hw: dict[str, Any] | None = None,
                    free_mb: int | None = None) -> tuple[int, dict[str, Any]]:
    """How many layers of this model fit on the GPU right now.

    Each layer on the GPU costs its share of the weights plus its share of the
    conversation memory (KV cache: context x KV heads x head size, f16). On top of
    that llama.cpp needs the output layer and a compute buffer. Whatever doesn't
    fit stays on the CPU. Returns (layers for -ngl, explanation)."""
    hw = hw or detect()
    info: dict[str, Any] = {"layers": 0, "of": None}
    if not hw.get("gpu_usable"):
        info["why"] = "no GPU backend in llama.cpp" if hw.get("gpus") else "no GPU found"
        return 0, info
    from . import gguf
    path = Path(model_path)
    try:
        meta = gguf.metadata(path)
        n_layers = int(meta["block_count"])
    except (OSError, ValueError, KeyError) as exc:
        info["why"] = f"couldn't read the model's layer count ({exc}); using all layers"
        return 99, info
    size_mb = path.stat().st_size / (1024 * 1024)
    emb = int(meta.get("embedding_length") or 4096)
    heads = int(meta.get("attention.head_count") or 32)
    kv_heads = int(meta.get("attention.head_count_kv") or heads)
    k_dim = int(meta.get("attention.key_length") or emb // max(1, heads))
    v_dim = int(meta.get("attention.value_length") or k_dim)
    kv_per_layer = ctx_size * kv_heads * (k_dim + v_dim) * 2 / (1024 * 1024)
    w_per_layer = size_mb / (n_layers + 1)                  # +1: embeddings / output layer
    free = free_mb if free_mb is not None else free_vram_mb()
    if free is None:
        free = int(hw.get("vram_mb", 0) * 0.85) - 1500        # unknown: assume the desktop uses some
    reserve = 600 + w_per_layer                              # compute buffer + output layer
    budget = free - reserve - 256                            # safety margin
    per_layer = w_per_layer + kv_per_layer
    fit = max(0, int(budget // per_layer)) if per_layer > 0 else 0
    info.update(of=n_layers, free_mb=int(free), per_layer_mb=round(per_layer, 1),
                kv_mb=round(kv_per_layer * n_layers), model_mb=round(size_mb))
    if fit >= n_layers:
        info.update(layers=n_layers, why="the whole model fits on the GPU")
        return 99, info
    info.update(layers=fit, why=f"{fit} of {n_layers} layers fit in {int(free)} MB of free VRAM; "
                                 "the rest run on the CPU")
    return fit, info
