"""Offline Wikipedia (no pictures) for agents to reference.

Format: Kiwix ZIM (``wikipedia_en_all_nopic_YYYY-MM.zim``) -- a single
compressed file with a built-in full-text index, read with python-libzim.

Smart Update (SPEC open question -- answer): ZIM files are rebuilt monthly
and have no official diff/patch format (zim-tools' old zimdiff/zimpatch were
dropped), so "download only the changes" isn't possible for this format.
What Smart Update does instead:
  * checks the mirror and skips the download entirely when there's no newer
    edition than the one installed;
  * resumes interrupted downloads instead of restarting (HTTP Range);
  * keeps the old edition usable until the new one finishes, then deletes it.
Wikimedia does publish daily "adds-changes" XML dumps, but applying those
means maintaining a full MediaWiki-XML pipeline and search index locally,
which costs far more disk and CPU than re-downloading a ~50 GB ZIM monthly.
"""
from __future__ import annotations

import logging
import re
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .. import paths
from ..functions.partials.web import html_to_text
from ..models.download import DownloadCancelled, fetch

log = logging.getLogger("jeeves.wikipedia")


class Wikipedia:
    def __init__(self, settings: Any, publish: Callable[[str, Any], None]) -> None:
        self.settings = settings
        self.publish = publish
        self._archive = None
        self._archive_path: Path | None = None
        self._cancel: threading.Event | None = None
        self.progress: dict[str, Any] = {"state": "idle"}

    @property
    def folder(self) -> Path:
        return paths.data_dir() / "wikipedia"

    def current_file(self) -> Path | None:
        name = self.settings.get("wikipedia.file")
        if name and (self.folder / name).is_file():
            return self.folder / name
        found = sorted(self.folder.glob("*.zim")) if self.folder.is_dir() else []
        return found[-1] if found else None

    def available(self) -> bool:
        return self.current_file() is not None and self._open() is not None

    def status(self) -> dict[str, Any]:
        f = self.current_file()
        return {"file": f.name if f else None, "size": f.stat().st_size if f else 0,
                "libzim": _libzim() is not None, "progress": self.progress}

    # ---- lookup ------------------------------------------------------------
    def _open(self):
        lz = _libzim()
        f = self.current_file()
        if lz is None or f is None:
            return None
        if self._archive is None or self._archive_path != f:
            self._archive = lz.reader.Archive(str(f))
            self._archive_path = f
        return self._archive

    def _candidates(self, archive: Any, query: str, n: int = 5) -> list[str]:
        """Paths of the articles the query may mean: titles that start like it, then the full-text hits."""
        lz = _libzim()
        paths_found: list[str] = []
        try:
            sugg = lz.suggestion.SuggestionSearcher(archive).suggest(query)
            paths_found += list(sugg.getResults(0, n))
        except Exception:
            pass
        try:
            search = lz.search.Searcher(archive).search(lz.search.Query().set_query(query))
            paths_found += list(search.getResults(0, n))
        except Exception:
            pass
        return list(dict.fromkeys(paths_found))

    def _read(self, archive: Any, path: str) -> tuple[str, str]:
        entry = archive.get_entry_by_path(path)
        if entry.is_redirect:
            entry = entry.get_redirect_entry()
        item = entry.get_item()
        return entry.title, html_to_text(bytes(item.content).decode("utf-8", "replace"))

    def lookup(self, query: str, max_chars: int = 6000, about: str = "") -> dict[str, Any] | None:
        """The article the query most likely means. "X may refer to:" pages are passed over (the first
        hit for a plain name is often one), and of the candidates the one whose title shares the most
        words with the query -- and with `about`, the question being answered -- is read."""
        archive = self._open()
        if archive is None:
            return None
        want = set(_words(query)) | set(_words(about))
        best: tuple[float, str, str] | None = None
        for rank, path in enumerate(self._candidates(archive, query)[:6]):
            try:
                title, text = self._read(archive, path)
            except Exception:
                continue
            if is_disambiguation(title, text):
                continue
            words = set(_words(title))
            score = len(want & words) / max(1, len(words)) - 0.05 * rank
            if title.strip().lower() == query.strip().lower():
                score += 2.0
            if best is None or score > best[0]:
                best = (score, title, text)
        if best is None:
            return None
        return {"title": best[1], "text": best[2][:max_chars]}

    # ---- download / update ---------------------------------------------------
    def latest(self) -> tuple[str, str]:
        mirror = self.settings.get("wikipedia.mirror")
        variant = self.settings.get("wikipedia.variant")
        with urllib.request.urlopen(urllib.request.Request(mirror, headers={"User-Agent": "Jeeves/0.1"}),
                                    timeout=30) as r:
            listing = r.read().decode(errors="replace")
        names = sorted(set(re.findall(rf'href="({re.escape(variant)}_\d{{4}}-\d{{2}}\.zim)"', listing)))
        if not names:
            raise RuntimeError(f"no {variant} editions found at {mirror}")
        return names[-1], mirror.rstrip("/") + "/" + names[-1]

    def update(self) -> None:
        if self._cancel is not None:
            return
        self._cancel = threading.Event()
        threading.Thread(target=self._update, daemon=True, name="jeeves-wikipedia").start()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def _set(self, **p: Any) -> None:
        self.progress = p
        self.publish("wikipedia", self.status())

    def _update(self) -> None:
        try:
            self._set(state="checking")
            name, url = self.latest()
            cur = self.current_file()
            if self.settings.get("wikipedia.smart_update", True) and cur is not None and cur.name == name:
                self._set(state="up_to_date", file=name)
                return
            self.folder.mkdir(parents=True, exist_ok=True)

            def prog(done: int, total: int) -> None:
                self._set(state="downloading", file=name, done=done, total=total)

            fetch(url, self.folder / name, prog, self._cancel)
            self.settings.set("wikipedia.file", name)
            self._archive = None
            for old in self.folder.glob("*.zim"):
                if old.name != name:
                    old.unlink(missing_ok=True)
            self._set(state="done", file=name)
        except DownloadCancelled:
            self._set(state="cancelled")
        except Exception as exc:
            log.exception("wikipedia update failed")
            self._set(state="error", error=str(exc))
        finally:
            self._cancel = None


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in
            ("the", "and", "for", "what", "who", "how", "was", "were", "are", "does", "did", "when", "where", "why")]


def is_disambiguation(title: str, text: str) -> bool:
    """A page that only lists what a name can mean ("Mercury may refer to:")."""
    if "(disambiguation)" in title.lower():
        return True
    return re.search(r"\b(?:may|can|could|might)(?: also)? (?:refer to|mean|stand for)\b\s*:?", text[:400], re.I) \
        is not None and len(re.findall(r"\n", text[:3000])) >= 4


def _libzim():
    try:
        import libzim  # type: ignore
        import libzim.reader  # noqa: F401
        import libzim.search  # noqa: F401
        import libzim.suggestion  # noqa: F401
        return libzim
    except Exception:
        return None
