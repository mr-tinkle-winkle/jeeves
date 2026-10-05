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


# NixOS user services get a minimal PATH and no driver library path, so look in the
# places the NVIDIA driver actually lives too
NVIDIA_SMI = ["/run/current-system/sw/bin/nvidia-smi", "/run/opengl-driver/bin/nvidia-smi", "/usr/bin/nvidia-smi"]
NVML_LIBS = ["libnvidia-ml.so.1", "/run/opengl-driver/lib/libnvidia-ml.so.1",
             "/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1", "/usr/lib64/libnvidia-ml.so.1",
             "/usr/lib/libnvidia-ml.so.1"]


def _nvml() -> list[dict[str, Any]] | None:
    """NVIDIA GPUs with total/free VRAM straight from the driver library (NVML), or
    None if it can't be loaded. Doesn't need nvidia-smi on PATH."""
    import ctypes
    lib = None
    for name in NVML_LIBS:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        return None

    class Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]
    try:
        if lib.nvmlInit_v2() != 0:
            return None
        try:
            count = ctypes.c_uint()
            if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
                return None
            out = []
            for i in range(count.value):
                handle = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(handle)) != 0:
                    continue
                buf = ctypes.create_string_buffer(96)
                lib.nvmlDeviceGetName(handle, buf, 96)
                mem = Mem()
                if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(mem)) != 0:
                    continue
                out.append({"name": buf.value.decode(errors="replace") or "NVIDIA GPU", "vendor": "nvidia",
                            "vram_mb": int(mem.total // (1024 * 1024)), "free_mb": int(mem.free // (1024 * 1024))})
            return out
        finally:
            lib.nvmlShutdown()
    except (AttributeError, OSError):
        return None


def _nvidia_smi_path() -> str | None:
    return shutil.which("nvidia-smi") or next((p for p in NVIDIA_SMI if os.access(p, os.X_OK)), None)


def _nvidia() -> list[dict[str, Any]]:
    found = _nvml()
    if found:
        return found
    smi = _nvidia_smi_path()
    if smi:
        try:
            text = subprocess.run([smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                                  capture_output=True, text=True, timeout=5).stdout
            out = []
            for line in text.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3 and parts[-2].isdigit():
                    out.append({"name": ",".join(parts[:-2]), "vendor": "nvidia", "vram_mb": int(parts[-2]),
                                "free_mb": int(parts[-1]) if parts[-1].isdigit() else None})
            if out:
                return out
        except (OSError, subprocess.TimeoutExpired):
            pass
    # driver loaded but no tools: at least say there is one (VRAM unknown)
    out = []
    for info in sorted(Path("/proc/driver/nvidia/gpus").glob("*/information")):
        try:
            model = next((ln.split(":", 1)[1].strip() for ln in info.read_text().splitlines()
                          if ln.startswith("Model:")), "NVIDIA GPU")
        except OSError:
            continue
        out.append({"name": model, "vendor": "nvidia", "vram_mb": 0, "vram_unknown": True})
    return out


def _gpus() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = list(_nvidia())
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


def keep_free(settings: Any) -> dict[str, float]:
    """The 'Minimum untouched' settings, cleaned up."""
    k = (settings.get("models.keep_free", {}) if settings is not None else {}) or {}
    num = lambda key, d: max(0.0, float(k.get(key, d) if k.get(key) is not None else d))  # noqa: E731
    return {"ram_gb": num("ram_gb", 4.0), "vram_gb": num("vram_gb", 1.0),
            "cpu_threads": int(num("cpu_threads", 1)), "gpu_percent": min(100.0, num("gpu_percent", 0))}


def budget(hw: dict[str, Any], keep: dict[str, float] | None) -> dict[str, Any]:
    """What the AIs may use: the hardware minus what must stay untouched. Same shape as
    detect(), plus 'total' (the real numbers), 'keep_free' and 'gpu_share' (the part of
    each model's layers allowed on the GPU)."""
    keep = keep or {"ram_gb": 0, "vram_gb": 0, "cpu_threads": 0, "gpu_percent": 0}
    out = dict(hw)
    out["total"] = {"ram_mb": hw["ram_mb"], "vram_mb": hw["vram_mb"], "cpu_threads": hw["cpu_threads"]}
    out["keep_free"] = keep
    out["ram_mb"] = max(0, int(hw["ram_mb"] - keep["ram_gb"] * 1024))
    out["vram_mb"] = max(0, int(hw["vram_mb"] - keep["vram_gb"] * 1024))
    out["cpu_threads"] = max(1, int(hw["cpu_threads"] - keep["cpu_threads"]))
    share = 1.0 - keep["gpu_percent"] / 100.0
    out["gpu_share"] = share
    out["gpu_usable"] = bool(hw.get("gpu_usable") and out["vram_mb"] > 0 and share > 0)
    if share <= 0 or out["vram_mb"] <= 0:
        out["whisper_gpu"] = []
    return out


# ---------------------------------------------------------------------------
# How each model fits
# ---------------------------------------------------------------------------

def fit(entry: catalog.ModelEntry, hw: dict[str, Any]) -> str:
    """'gpu' (fits in VRAM with a GPU backend), 'cpu' (runs from RAM), 'slow' (fits in
    RAM but too big to be responsive on a CPU) or 'no' (not enough memory)."""
    need = max(entry.ram_mb, 1)
    share = float(hw.get("gpu_share", 1.0))
    budgeted = "keep_free" in hw                   # ram_mb is already what's left for the AIs
    if entry.kind == "llm" and hw.get("gpu_usable") and share >= 0.999 and \
            entry.size_mb + 1000 <= hw["vram_mb"] * 0.95:
        return "gpu"
    on_gpu = 0.0                                   # part of the model that can sit in VRAM
    if entry.kind == "llm" and hw.get("gpu_usable") and entry.size_mb:
        on_gpu = max(0.0, min(share, (hw["vram_mb"] * 0.95 - 1000) / entry.size_mb))
    if need * (1 - on_gpu * 0.8) > hw["ram_mb"] * (1.0 if budgeted else 0.75):
        return "no"
    if entry.kind == "llm":
        active = entry.active_b or entry.params_b
        limit = (8 if hw["cpu_threads"] >= 12 else 4) / max(0.15, 1 - on_gpu)
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
    where = "your GPU" if hw.get("gpu_usable") and hw.get("gpu_share", 1) >= 0.999 else \
        "your GPU and CPU" if hw.get("gpu_usable") else "your CPU"
    out: dict[str, Any] = {}

    def pick(role: str, entry: catalog.ModelEntry | None, why: str) -> None:
        if entry is not None:
            out[role] = {"id": entry.id, "name": entry.name, "why": why}

    pick("wake", catalog.get("vosk-small-en"), "Light enough to listen all the time.")
    stts = catalog.of_kind("stt")
    if hw.get("whisper_gpu") and hw["vram_mb"] >= 3000 and hw.get("gpu_share", 1) >= 0.5:
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
    vals = [g["free_mb"] for g in _nvidia() if g.get("free_mb") is not None]
    if vals:
        best = max(vals)
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
                    free_mb: int | None = None, keep_vram_mb: int = 0, gpu_share: float = 1.0,
                    fixed: int | None = None) -> tuple[int, dict[str, Any]]:
    """How many layers of this model fit on the GPU right now.

    Each layer on the GPU costs its share of the weights plus its share of the
    conversation memory (KV cache: context x KV heads x head size, f16). On top of
    that llama.cpp needs the output layer and a compute buffer. Whatever doesn't
    fit stays on the CPU. keep_vram_mb stays free on top; gpu_share caps the part
    of the layers allowed on the GPU; fixed = a hand-set layer count (still capped).
    Returns (layers for -ngl, explanation)."""
    hw = hw or detect()
    info: dict[str, Any] = {"layers": 0, "of": None}
    if gpu_share <= 0:
        info["why"] = "Minimum untouched GPU is 100%: everything runs on the CPU"
        return 0, info
    if fixed == 0:
        info["why"] = "CPU only (set by hand)"
        return 0, info
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
        return (fixed if fixed is not None else 99), info
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
    room = free - keep_vram_mb - reserve - 256               # safety margin
    per_layer = w_per_layer + kv_per_layer
    fit = max(0, int(room // per_layer)) if per_layer > 0 else 0
    info.update(of=n_layers, free_mb=int(free), per_layer_mb=round(per_layer, 1),
                kv_mb=round(kv_per_layer * n_layers), model_mb=round(size_mb), kept_free_mb=int(keep_vram_mb))
    limits = []
    if gpu_share < 1.0:
        cap = int(n_layers * gpu_share)
        if cap < fit:
            fit = cap
            limits.append(f"Minimum untouched GPU allows {round(gpu_share * 100)}% of the layers")
    if fixed is not None and fixed < fit:
        fit = fixed
        limits.append("set by hand")
    kept = f" (keeping {int(keep_vram_mb)} MB untouched)" if keep_vram_mb else ""
    if fit >= n_layers:
        info.update(layers=n_layers, why="the whole model fits on the GPU" + kept)
        return 99, info
    info.update(layers=fit, why=f"{fit} of {n_layers} layers on the GPU"
                + (f" ({'; '.join(limits)})" if limits else f": that's what fits in {int(free)} MB of free VRAM{kept}")
                + "; the rest run on the CPU")
    return fit, info


def available_ram_mb() -> int | None:
    """MemAvailable: what can be used right now without pushing anything into swap."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None
