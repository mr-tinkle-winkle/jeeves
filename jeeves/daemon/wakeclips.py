"""The last wake-ups, kept as clips you can listen to (Training > Recent wake-ups).

Every listen a call name opened is kept as a short clip -- the name and what followed, up to 12 s --
with what the wake word model heard (each word's confidence and length), what speech recognition heard,
and what happened. The last 30 are kept (wake_word.clips_kept).

Marking one teaches Jeeves:
  - "it was a call" (said: what you actually said, if speech recognition got it wrong): the clip and its
    text join your voice recordings (Training > Your voice: Whisper's vocabulary prompt and the accuracy
    check use them), and its name length counts toward what a real call sounds like;
  - "not a call" (said: what it really was -- "guh", "cheese"): those words become decoys in the wake
    word model's grammar, so next time they're decoded as themselves instead of as the name; and its
    length counts toward what a false wake sounds like.
From both kinds, once there are a few of each, the shortest a name can be said (and still be taken
as the name) is set between the two: wake_word.learned_min_seconds.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .. import paths
from ..util import normalize
from .audio import to_wav

MAX_SECONDS = 12
RATE = 16000


class WakeClips:
    def __init__(self, settings: Any, training: Any = None) -> None:
        self.settings = settings
        self.training = training
        self._lock = threading.Lock()

    @property
    def folder(self) -> Path:
        return paths.state_dir() / "wake_clips"

    @property
    def index_file(self) -> Path:
        return self.folder / "index.json"

    def _load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.index_file.read_text())
        except (OSError, ValueError):
            return []

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.index_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        tmp.replace(self.index_file)

    # ------------------------------------------------------------------ keeping
    def add(self, pcm: bytes, agent: str | None, source: str, name: str = "", conf: float = 0.0,
            words: list[Any] | None = None) -> str:
        """A new clip (newest first); the audio is written in the background. -> its id."""
        cid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        pcm = pcm[: MAX_SECONDS * RATE * 2]
        rec = {"id": cid, "time": time.time(), "agent": agent, "source": source, "name": name,
               "conf": round(float(conf), 2), "words": [list(w) for w in (words or [])], "heard": "",
               "outcome": "listening", "verdict": None, "said": "", "file": ""}
        with self._lock:
            items = [rec] + self._load()
            keep = int(self.settings.get("wake_word.clips_kept", 30) or 30)
            for old in items[keep:]:
                for f in self.folder.glob(f"{old['id']}.*"):
                    f.unlink(missing_ok=True)
            self._save(items[:keep])
        threading.Thread(target=self._write, args=(cid, pcm), daemon=True, name="jeeves-wake-clip").start()
        return cid

    def _write(self, cid: str, pcm: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{cid}.raw").write_bytes(pcm)       # (for training; the mp3 is for listening)
        out = self.folder / f"{cid}.mp3"
        name = ""
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(RATE), "-ac", "1",
                                    "-i", "-", "-b:a", "48k", str(out)], input=pcm, capture_output=True, timeout=30)
                if r.returncode == 0:
                    name = out.name
            except (OSError, subprocess.SubprocessError):
                pass
        if not name:
            (self.folder / f"{cid}.wav").write_bytes(to_wav(pcm))
            name = f"{cid}.wav"
        self.update(cid, file=name)

    def update(self, cid: str | None, **fields: Any) -> None:
        if not cid:
            return
        with self._lock:
            items = self._load()
            for it in items:
                if it["id"] == cid:
                    it.update(fields)
            self._save(items)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for it in self._load():
            it = dict(it)
            it["path"] = str(self.folder / it["file"]) if it.get("file") else ""
            out.append(it)
        return out

    # ------------------------------------------------------------------ learning
    def mark(self, cid: str, good: bool, said: str = "") -> dict[str, Any]:
        items = self._load()
        it = next((i for i in items if i["id"] == cid), None)
        if it is None:
            raise ValueError("that clip is gone (only the last ones are kept)")
        said = (said or "").strip()
        self.update(cid, verdict="good" if good else "bad", said=said)
        learned: dict[str, Any] = {}
        raw = self.folder / f"{cid}.raw"
        text = said or (it.get("heard") if good else "")
        if text and raw.exists() and self.training is not None:
            self.training.save_recording(text, raw.read_bytes())
            learned["recording"] = text
        if not good and said:
            learned["decoys"] = self.add_decoys(said)
        learned.update(self.relearn_length())
        return learned

    def _call_words(self) -> set[str]:
        out: set[str] = set()
        for a in (self.settings.get("agents", {}) or {}).values():
            for n in list(a.get("call_names", []) or []) + [a.get("name", "")]:
                out.update(normalize(n).split())
        return out

    def add_decoys(self, said: str) -> list[str]:
        """What a false wake really was ("guh", "cheese") -> words the wake grammar decodes as themselves."""
        own = self._call_words()
        new = [w for w in normalize(said).split() if w and w not in own and len(w) <= 20]
        have = list(self.settings.get("wake_word.extra_decoys", []) or [])
        added = [w for w in dict.fromkeys(new) if w not in have]
        if added:
            self.settings.set("wake_word.extra_decoys", (have + added)[-200:])
        return added

    def relearn_length(self) -> dict[str, Any]:
        """The shortest a name word can be said and still count, set between how long the real calls'
        names were and how long the false wakes' were -- once there are three of each."""
        own = self._call_words()

        def lengths(verdict: str) -> list[float]:
            out = []
            for it in self._load():
                if it.get("verdict") != verdict:
                    continue
                for w in it.get("words") or []:
                    if len(w) >= 3 and w[0] in own:
                        out.append(float(w[2]))
            return out
        good, bad = lengths("good"), lengths("bad")
        if len(good) < 3 or len(bad) < 3:
            return {}
        g, b = statistics.median(good), statistics.median(bad)
        if b >= g:
            return {}                     # (false wakes as long as real ones: length can't tell them apart)
        cut = round((g + b) / 2, 3)
        self.settings.set("wake_word.learned_min_seconds", cut)
        return {"min_seconds": cut}
