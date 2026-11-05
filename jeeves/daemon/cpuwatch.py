"""`jeeves cpu [SECONDS]`: what the daemon spends its CPU on, to find spikes.

For the given time the daemon watches itself: every 50 ms it reads how much CPU each of its threads
used and charges it to the code that thread is running (its Python stack); every program it starts
is counted with the code that started it; and its child processes (model servers, audio capture,
the overlay) are measured too. The report lists the busy seconds, the code that used the most, and
the programs started -- the thing to send when Jeeves makes the computer busy."""
from __future__ import annotations

import collections
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

TICK = os.sysconf("SC_CLK_TCK")
HERE = str(Path(__file__).resolve().parent.parent)


def _ticks(path: str) -> int:
    try:
        f = Path(path).read_text().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except (OSError, ValueError, IndexError):
        return 0


def _children(root: int) -> dict[int, str]:
    """Every descendant process: pid -> name."""
    parents: dict[int, tuple[int, str]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = Path(f"/proc/{d}/stat").read_text()
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            parents[int(d)] = (int(stat.rsplit(")", 1)[1].split()[1]), name)
        except (OSError, ValueError, IndexError):
            continue
    out: dict[int, str] = {}
    frontier = [root]
    while frontier:
        p = frontier.pop()
        for c, (pp, name) in parents.items():
            if pp == p and c not in out:
                out[c] = name
                frontier.append(c)
    return out


def _where(frame: Any) -> str:
    """The innermost few lines of Jeeves' own code in a stack."""
    stack = [f for f in traceback.extract_stack(frame) if f.filename.startswith(HERE)]
    if not stack:
        stack = traceback.extract_stack(frame)[-2:]
    return " <- ".join(f"{Path(f.filename).stem}.{f.name}:{f.lineno}" for f in reversed(stack[-3:]))


def profile(seconds: float = 30.0, interval: float = 0.05) -> dict[str, Any]:
    seconds = max(1.0, min(600.0, float(seconds)))
    me = threading.get_native_id()
    pid = os.getpid()
    names = {t.native_id: t.name for t in threading.enumerate()}
    spawned: collections.Counter[str] = collections.Counter()
    spawn_where: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    real_init = subprocess.Popen.__init__

    def counting_init(self: Any, args: Any, *a: Any, **kw: Any) -> None:
        prog = Path(str(args[0] if isinstance(args, (list, tuple)) and args else args).split()[0]).name
        spawned[prog] += 1
        spawn_where[prog][_where(sys._getframe(1))] += 1
        real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = counting_init          # type: ignore[method-assign]
    import gc
    gc_runs: list[tuple[int, float]] = []
    gc_t0 = [0.0]

    def on_gc(phase: str, info: dict[str, Any]) -> None:
        if phase == "start":
            gc_t0[0] = time.perf_counter()
        elif info.get("generation") == 2:
            gc_runs.append((info.get("collected", 0), time.perf_counter() - gc_t0[0]))
    gc.callbacks.append(on_gc)
    by_code: collections.Counter[str] = collections.Counter()
    per_second: list[float] = []
    second_code: list[collections.Counter[str]] = []
    child_ticks: collections.Counter[str] = collections.Counter()
    last: dict[int, int] = {}
    kids_last: dict[int, int] = {}
    start = time.monotonic()
    sec_start, sec_ticks, sec_code = start, 0, collections.Counter()
    next_kids = start
    try:
        while (now := time.monotonic()) - start < seconds:
            frames = sys._current_frames()
            idents = {t.native_id: t.ident for t in threading.enumerate()}
            for task in os.listdir(f"/proc/{pid}/task"):
                tid = int(task)
                if tid == me:
                    continue
                t = _ticks(f"/proc/{pid}/task/{task}/stat")
                d = t - last.get(tid, t)
                last[tid] = t
                if d <= 0:
                    continue
                fr = frames.get(idents.get(tid, -1))
                where = f"[{names.get(tid, 'native thread')}] " + (_where(fr) if fr is not None else "(not Python)")
                by_code[where] += d
                sec_code[where] += d
                sec_ticks += d
            if now >= next_kids:
                next_kids = now + 0.5
                for cpid, name in _children(pid).items():
                    t = _ticks(f"/proc/{cpid}/stat")
                    d = t - kids_last.get(cpid, t)
                    kids_last[cpid] = t
                    if d > 0:
                        child_ticks[name] += d
                        sec_code[f"(program) {name}"] += d
                        sec_ticks += d
            if now - sec_start >= 1.0:
                per_second.append(100.0 * sec_ticks / TICK / (now - sec_start))
                second_code.append(sec_code)
                sec_start, sec_ticks, sec_code = now, 0, collections.Counter()
            time.sleep(interval)
    finally:
        subprocess.Popen.__init__ = real_init          # type: ignore[method-assign]
        gc.callbacks.remove(on_gc)
    total = time.monotonic() - start
    ordered = sorted(per_second)
    median = ordered[len(ordered) // 2] if ordered else 0.0
    spikes = []
    for i, pct in enumerate(per_second):
        if pct > max(median * 2, median + 15):
            top = ", ".join(f"{w} {100 * n / TICK:.0f}%" for w, n in second_code[i].most_common(3))
            spikes.append({"second": i, "cpu": round(pct), "top": top})
    return {
        "seconds": round(total, 1),
        "average_cpu": round(sum(per_second) / max(1, len(per_second)), 1),
        "median_cpu": round(median, 1),
        "per_second": [round(x) for x in per_second],
        "spikes": spikes,
        "code": [{"where": w, "cpu": round(100 * n / TICK / total, 2)} for w, n in by_code.most_common(15)],
        "programs": [{"name": n, "cpu": round(100 * t / TICK / total, 2)} for n, t in child_ticks.most_common(10)],
        "started": [{"program": p, "times": n, "from": spawn_where[p].most_common(2)} for p, n in spawned.most_common(10)],
        "full_gc": {"runs": len(gc_runs), "longest_ms": round(1000 * max((d for _c, d in gc_runs), default=0.0), 1)},
    }


def format_report(r: dict[str, Any]) -> str:
    lines = [f"Jeeves' daemon over {r['seconds']} s: {r['average_cpu']}% of one core on average "
             f"(median second {r['median_cpu']}%; its own programs included).", ""]
    lines.append("Each second: " + " ".join(str(x) for x in r["per_second"]))
    lines.append("")
    if r["spikes"]:
        lines.append("Spikes:")
        for s in r["spikes"]:
            lines.append(f"  second {s['second']}: {s['cpu']}%  ({s['top']})")
    else:
        lines.append("No spikes while watching.")
    lines += ["", "Most CPU, by code (% of one core):"]
    lines += [f"  {c['cpu']:6.2f}%  {c['where']}" for c in r["code"]]
    if r["programs"]:
        lines += ["", "Its programs (% of one core):"]
        lines += [f"  {p['cpu']:6.2f}%  {p['name']}" for p in r["programs"]]
    if r["started"]:
        lines += ["", "Programs it started:"]
        for s in r["started"]:
            src = "; ".join(f"{w} ×{n}" for w, n in s["from"])
            lines.append(f"  {s['program']} ×{s['times']}  from {src}")
    g = r.get("full_gc") or {}
    lines += ["", f"Full garbage collections: {g.get('runs', 0)}, longest {g.get('longest_ms', 0)} ms."]
    return "\n".join(lines)
