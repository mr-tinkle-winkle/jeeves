"""Training the agents.

STT (your voice):
  * A recording session shows you phrases to read (your agents' names, your
    functions' keywords, everyday commands). Each recording is kept with its
    text in ``~/.local/share/jeeves/training/stt/`` as a dataset.
  * What is used live: every word in those phrases, plus any word you add to
    the vocabulary list, is passed to Whisper as its initial prompt -- the
    supported way to bias whisper.cpp toward names and jargon -- and each
    recording is transcribed to measure accuracy before/after.
  * Full fine-tuning of Whisper weights needs a GPU training run outside
    Jeeves; ``export_dataset`` writes the dataset in the common
    ``metadata.csv`` + ``wavs/`` layout those scripts expect.

Intent (what you mean):
  * Training phrases: "when I say X, run function F with these arguments".
  * Ratings from the History page.
  Both are shown to the intent model as worked examples in the Dictionary.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from .. import paths
from .audio import to_wav

GENERIC_PHRASES = [
    "Set a timer for ten minutes.",
    "What's the weather like tomorrow?",
    "Open all my apps.",
    "Remember forever that my locker code is four two one seven.",
    "End extended prompt mode.",
    "Proceed.",
    "Summarize the last ten minutes.",
    "Read the text in the middle of the screen.",
]


class Training:
    def __init__(self, settings: Any, registry: Any) -> None:
        self.settings = settings
        self.registry = registry

    @property
    def folder(self) -> Path:
        return paths.data_dir() / "training"

    # ---- STT -----------------------------------------------------------------
    def phrases(self) -> list[str]:
        out = []
        for agent in (self.settings.get("agents", {}) or {}).values():
            for name in agent.get("call_names", []):
                out.append(f"{name}, what time is it?")
        for f in self.registry.all("full"):
            kws = self.registry.keywords(f)
            if kws:
                out.append(f"{kws[0].capitalize()}.")
        return out + GENERIC_PHRASES

    def save_recording(self, text: str, pcm: bytes) -> dict[str, Any]:
        d = self.folder / "stt" / "wavs"
        d.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex[:10]
        (d / f"{rid}.wav").write_bytes(to_wav(pcm))
        meta = self.folder / "stt" / "metadata.jsonl"
        item = {"id": rid, "text": text, "time": time.time()}
        with open(meta, "a") as f:
            f.write(json.dumps(item) + "\n")
        return item

    def recordings(self) -> list[dict[str, Any]]:
        meta = self.folder / "stt" / "metadata.jsonl"
        try:
            return [json.loads(line) for line in meta.read_text().splitlines() if line.strip()]
        except OSError:
            return []

    def vocabulary(self) -> list[str]:
        words = list(self.settings.get("training.stt_vocabulary", []) or [])
        for agent in (self.settings.get("agents", {}) or {}).values():
            words += agent.get("call_names", [])
        seen, out = set(), []
        for w in words:
            if w and w.lower() not in seen:
                seen.add(w.lower())
                out.append(w)
        return out

    def initial_prompt(self) -> str:
        vocab = self.vocabulary()
        return ("Vocabulary: " + ", ".join(vocab) + ".") if vocab else ""

    def evaluate(self, stt: Any) -> dict[str, Any]:
        """Word error rate of the current STT model over the recordings, with and
        without the vocabulary prompt."""
        from .audio import wav_to_pcm
        recs = self.recordings()
        if not recs:
            return {"recordings": 0}
        totals = {"plain": [0, 0], "prompted": [0, 0]}
        prompt = self.initial_prompt()
        for r in recs[-40:]:
            wav = self.folder / "stt" / "wavs" / f"{r['id']}.wav"
            if not wav.exists():
                continue
            pcm, _ = wav_to_pcm(wav.read_bytes())
            for key, p in (("plain", ""), ("prompted", prompt)):
                hyp = stt.transcribe(pcm, prompt=p)
                errs, n = _wer(r["text"], hyp)
                totals[key][0] += errs
                totals[key][1] += n
        return {"recordings": len(recs),
                **{k: round(e / n, 3) if n else None for k, (e, n) in totals.items()}}

    def export_dataset(self, dest: str) -> str:
        out = Path(dest).expanduser()
        (out / "wavs").mkdir(parents=True, exist_ok=True)
        lines = []
        for r in self.recordings():
            src = self.folder / "stt" / "wavs" / f"{r['id']}.wav"
            if src.exists():
                shutil.copy(src, out / "wavs" / src.name)
                lines.append(f"{r['id']}|{r['text']}")
        (out / "metadata.csv").write_text("\n".join(lines) + "\n")
        return str(out)

    # ---- intent ----------------------------------------------------------------
    def intent_file(self) -> Path:
        return self.folder / "intent.json"

    def intent_phrases(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.intent_file().read_text())
        except (OSError, ValueError):
            return []

    def add_intent_phrase(self, text: str, function: str, args: dict[str, Any] | None = None,
                          agent: str | None = None) -> dict[str, Any]:
        if self.registry.get(function) is None:
            raise ValueError(f"unknown function '{function}'")
        items = self.intent_phrases()
        item = {"id": uuid.uuid4().hex[:8], "text": text, "function": function, "args": args or {}, "agent": agent}
        items.append(item)
        self.intent_file().parent.mkdir(parents=True, exist_ok=True)
        self.intent_file().write_text(json.dumps(items, indent=2))
        return item

    def remove_intent_phrase(self, pid: str) -> None:
        items = [i for i in self.intent_phrases() if i["id"] != pid]
        self.intent_file().write_text(json.dumps(items, indent=2))

    def intent_examples(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for i in self.intent_phrases():
            args = json.dumps(i.get("args") or {})
            out.setdefault(i["function"], []).append({"text": i["text"], "good": True,
                                                      "comment": f"training phrase, args {args}"})
        return out


def _wer(ref: str, hyp: str) -> tuple[int, int]:
    from ..util import normalize
    r, h = normalize(ref).split(), normalize(hyp).split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)
