"""Hybrid Models: the full set of models while the computer is otherwise quiet, a lighter set as
soon as something else (a game, a render, a compile) needs the CPU, GPU or memory -- and back
again once it's done.

Every few seconds the model watcher samples what *other* programs use:
  * CPU: the share of all cores busy outside Jeeves and its model servers (/proc);
  * GPU: how busy the GPU is (nvidia-smi or amdgpu's gpu_busy_percent), only counted while
    Jeeves itself isn't working -- its own answers would look like load otherwise;
  * RAM: how much is still available (/proc/meminfo).
When any of them stays over its limit for a few seconds, the light set takes over and the full
models are unloaded (ModelManager.prune). When everything stays calm for longer, the full set
comes back (loaded again on the next request).

Light set per kind (Settings > Models > Hybrid Models): "auto" = the best downloaded model of
that kind that needs well under half the memory of the normal one; "same" = keep the normal one;
"off" (vision only) = no vision model, screens are read with OCR; or a model id.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import catalog

log = logging.getLogger("jeeves.models")

HYBRID_KINDS = ("stt", "intent", "local_response", "vision")
LIGHT_SHARE = 0.6           # "auto": at most this share of the normal model's memory


def _proc_tree(root: int) -> set[int]:
    parents: dict[int, int] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = Path(f"/proc/{d}/stat").read_text()
            parents[int(d)] = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
    tree, frontier = {root}, [root]
    while frontier:
        p = frontier.pop()
        kids = [c for c, pp in parents.items() if pp == p and c not in tree]
        tree.update(kids)
        frontier += kids
    return tree


def _pid_ticks(pid: int) -> int:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])           # utime + stime
    except (OSError, ValueError, IndexError):
        return 0


def _cpu_ticks() -> tuple[int, int]:
    """(busy, total) jiffies over all cores."""
    parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    vals = [int(x) for x in parts[:8]]
    idle = vals[3] + vals[4]
    return sum(vals) - idle, sum(vals)


def gpu_busy() -> float | None:
    """GPU utilization in percent (the busiest GPU), or None if it can't be read."""
    best: float | None = None
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        try:
            best = max(best or 0.0, float((card / "device" / "gpu_busy_percent").read_text()))
        except (OSError, ValueError):
            continue
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=3).stdout
            vals = [float(v) for v in out.split() if v.strip().replace(".", "", 1).isdigit()]
            if vals:
                best = max(best or 0.0, max(vals))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return best


def ram_available_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


class Usage:
    """What the rest of the computer is using, sampled between calls."""

    def __init__(self) -> None:
        self._last: tuple[int, int, int] | None = None      # busy, total, ours
        self._tree: set[int] = set()
        self._tree_at = 0.0

    def sample(self, busy_self: bool) -> dict[str, float | None]:
        now = time.time()
        if now - self._tree_at > 30:
            self._tree, self._tree_at = _proc_tree(os.getpid()), now
        busy, total = _cpu_ticks()
        ours = sum(_pid_ticks(p) for p in self._tree)
        cpu = None
        if self._last is not None and total > self._last[1]:
            d_busy, d_total, d_ours = busy - self._last[0], total - self._last[1], ours - self._last[2]
            cpu = max(0.0, min(100.0, 100.0 * (d_busy - max(0, d_ours)) / d_total))
        self._last = (busy, total, ours)
        return {"cpu_others": cpu, "gpu": None if busy_self else gpu_busy(), "ram_free_mb": ram_available_mb()}


class Hybrid:
    def __init__(self, settings: Any, sampler: Callable[[bool], dict[str, Any]] | None = None) -> None:
        self.settings = settings
        self.sampler = sampler or Usage().sample
        self.light = False
        self.reason = ""
        self.last: dict[str, Any] = {}
        self._over_since: float | None = None
        self._calm_since: float | None = None

    def cfg(self, key: str, default: Any) -> Any:
        return self.settings.get(f"models.hybrid.{key}", default)

    def enabled(self) -> bool:
        return bool(self.cfg("enabled", True))

    def step(self, busy_self: bool = False, now: float | None = None) -> bool:
        """Sample and switch when due. True when the set in use changed."""
        now = time.time() if now is None else now
        if not self.enabled():
            changed, self.light, self.reason = self.light, False, ""
            return changed
        u = self.sampler(busy_self)
        self.last = u
        why = []
        if u.get("cpu_others") is not None and u["cpu_others"] >= float(self.cfg("cpu_percent", 60)):
            why.append(f"other programs use {u['cpu_others']:.0f}% of the CPU")
        if u.get("gpu") is not None and u["gpu"] >= float(self.cfg("gpu_percent", 50)):
            why.append(f"the GPU is {u['gpu']:.0f}% busy")
        if u.get("ram_free_mb") is not None and u["ram_free_mb"] < float(self.cfg("ram_free_gb", 3.0)) * 1024:
            why.append(f"only {u['ram_free_mb'] / 1024:.1f} GB of RAM is free")
        if why:
            self._calm_since = None
            self._over_since = self._over_since or now
            if not self.light and now - self._over_since >= float(self.cfg("switch_after", 8)):
                self.light, self.reason = True, "; ".join(why)
                log.info("Hybrid Models: switching to the light models (%s)", self.reason)
                return True
            if self.light:
                self.reason = "; ".join(why)
            return False
        self._over_since = None
        if busy_self and u.get("gpu") is None and self.light:
            return False                         # no GPU reading while Jeeves works: don't call it calm yet
        self._calm_since = self._calm_since or now
        if self.light and now - self._calm_since >= float(self.cfg("back_after", 45)):
            self.light, self.reason = False, ""
            log.info("Hybrid Models: things calmed down; back to the full models")
            return True
        return False

    def model_for(self, kind: str, normal: str | None, installed: Callable[[catalog.ModelEntry], bool]
                  ) -> str | None:
        """The model to use for this kind right now."""
        if not self.light or kind not in HYBRID_KINDS:
            return normal
        choice = self.cfg(kind, "off" if kind == "vision" else "auto")
        if choice in (None, "", "same"):
            return normal
        if choice == "off":
            return None if kind == "vision" else normal
        if choice != "auto":
            entry = catalog.get(choice)
            return choice if entry is not None and installed(entry) else normal
        return light_auto(kind, normal, installed)


def light_auto(kind: str, normal: str | None, installed: Callable[[catalog.ModelEntry], bool]) -> str | None:
    base = catalog.get(normal)
    if base is None:
        return normal
    if kind == "stt":
        pool = catalog.of_kind("stt")
    elif kind == "vision":
        pool = [e for e in catalog.of_kind("llm") if e.vision]
    else:
        pool = [e for e in catalog.of_kind("llm") if not e.vision]
    smaller = [e for e in pool if e.id != base.id and e.engine == base.engine and installed(e) and
               e.ram_mb <= base.ram_mb * LIGHT_SHARE]
    if not smaller:
        return None if kind == "vision" else normal
    return max(smaller, key=lambda e: (e.quality, e.speed, e.ram_mb)).id
