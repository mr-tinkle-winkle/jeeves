"""Resumable downloads with progress (models and Wikipedia)."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from .. import paths
from .catalog import ModelEntry

UA = "Jeeves/0.1"
CHUNK = 1 << 20


class DownloadCancelled(Exception):
    pass


def fetch(url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
          cancel: threading.Event | None = None, sha256: str | None = None) -> Path:
    """Download url -> dest, resuming a previous partial ``dest.part``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Range": f"bytes={have}-"} if have else {})})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:       # already complete
            part.replace(dest)
            return dest
        raise
    with resp:
        if have and resp.status != 206:   # server ignored Range: start over
            have = 0
            part.unlink(missing_ok=True)
        total = int(resp.headers.get("Content-Length") or 0) + have
        done = have
        last = 0.0
        with open(part, "ab") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress and time.time() - last > 0.25:
                    progress(done, total)
                    last = time.time()
    if progress:
        progress(done, total)
    if sha256:
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(CHUNK), b""):
                h.update(block)
        if h.hexdigest() != sha256:
            part.unlink(missing_ok=True)
            raise ValueError(f"checksum mismatch for {url}")
    part.replace(dest)
    return dest


def resolve_hf(repo: str, suffix: str, prefix: str = "") -> str:
    """The download URL of the file in a Hugging Face repo whose name ends with suffix (and starts
    with prefix, if given; otherwise vision projectors are skipped). Top-level files first; split
    multi-part files are skipped."""
    import json
    req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        files = [s["rfilename"] for s in json.loads(r.read().decode()).get("siblings", [])]
    want = suffix.lower()
    pre = prefix.lower()
    hits = [f for f in files if f.lower().endswith(want) and "-of-0" not in f
            and (f.rsplit("/", 1)[-1].lower().startswith(pre) if pre else "mmproj" not in f.lower())]
    if not hits:
        raise FileNotFoundError(f"no *{suffix} file in {repo}")
    hits.sort(key=lambda f: (f.count("/"), len(f)))
    return f"https://huggingface.co/{repo}/resolve/main/{hits[0]}"


def model_dir(entry: ModelEntry) -> Path:
    # presets of a multi-speaker voice share its download
    return paths.models_dir() / entry.kind / (entry.shares or entry.id)


def is_installed(entry: ModelEntry) -> bool:
    if entry.builtin and not entry.files:
        return True
    if not entry.files:     # e.g. kokoro voices live inside the kokoro model
        from .catalog import get
        parent = get(entry.tts_model) if entry.tts_model else None
        return parent is not None and is_installed(parent)
    d = model_dir(entry)
    return (d / ".complete").exists()


def install(entry: ModelEntry, progress: Callable[[dict[str, Any]], None] | None = None,
            cancel: threading.Event | None = None) -> Path:
    d = model_dir(entry)
    d.mkdir(parents=True, exist_ok=True)
    total_files = len(entry.files)
    for i, f in enumerate(entry.files):
        target = d / (Path(f.url).name if f.unzip else f.path)

        def report(done: int, total: int, i=i) -> None:
            if progress:
                progress({"model": entry.id, "file": i + 1, "files": total_files, "done": done, "total": total})

        url = f.url or resolve_hf(f.hf_repo, f.hf_suffix, f.hf_prefix)
        try:
            fetch(url, target, report, cancel, f.sha256)
        except urllib.error.HTTPError as exc:
            if exc.code != 404 or not f.hf_repo or not f.url:
                raise
            # the file was renamed upstream: look it up in the repo
            fetch(resolve_hf(f.hf_repo, f.hf_suffix, f.hf_prefix), target, report, cancel, f.sha256)
        if f.unzip:
            with zipfile.ZipFile(target) as z:
                z.extractall(d)
            target.unlink(missing_ok=True)
            # vosk zips contain one top folder; flatten it
            subs = [p for p in d.iterdir() if p.is_dir()]
            if len(subs) == 1 and not (d / "am").exists():
                for item in subs[0].iterdir():
                    shutil.move(str(item), d / item.name)
                subs[0].rmdir()
    (d / ".complete").write_text(str(time.time()))
    return d


def uninstall(entry: ModelEntry) -> None:
    d = model_dir(entry)
    if d.exists():
        shutil.rmtree(d)


def disk_usage(entry: ModelEntry) -> int:
    d = model_dir(entry)
    if not d.exists():
        return 0
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file())


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def env_threads(n: int) -> int:
    return n if n > 0 else max(1, (os.cpu_count() or 4) - 1)
