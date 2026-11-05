"""Jellyfin: finding shows, movies and books on your own server (the Jellyfin settings).

Signs in with an API key (Jellyfin: Dashboard > API Keys) as the user you name. Finds what was asked for
by Jellyfin's own search, then picks the best-named match of the kind asked for:

  - a show: the episode you named ("season 2 episode 5"), else the next one to watch (Jellyfin's Next Up),
    else the first; it plays in Jeeves' player from where you left off;
  - a movie: plays the same way;
  - a book: the file is downloaded once (to ~/.cache/jeeves/jellyfin) and opened in your book reader;
  - an audiobook or a song: plays in the player.

Played in the player, Jellyfin is told where you got to now and then, so its "continue watching" and Next
Up stay right.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ...util import normalize, similarity
from ..base import FunctionError

KINDS = {"show": ["Series"], "movie": ["Movie"], "book": ["Book"], "audiobook": ["AudioBook"],
         "music": ["MusicAlbum", "Audio"], "episode": ["Episode"],
         "any": ["Series", "Movie", "Book", "AudioBook", "Episode", "MusicAlbum", "Audio"]}
TICKS = 10_000_000                       # Jellyfin counts time in 100 ns ticks


class Jellyfin:
    def __init__(self, url: str, api_key: str, user: str = "", timeout: float = 10.0) -> None:
        if not url or not api_key:
            raise FunctionError("Jellyfin isn't set up: add its address and an API key in the Jellyfin settings")
        self.url = url.rstrip("/")
        if not re.match(r"https?://", self.url):
            self.url = "http://" + self.url
        self.key, self.user_name, self.timeout = api_key, user, timeout
        self._uid = ""

    # ------------------------------------------------------------------ HTTP
    def get(self, path: str, **params: Any) -> Any:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
        req = urllib.request.Request(f"{self.url}{path}{'?' + q if q else ''}",
                                     headers={"Authorization": f'MediaBrowser Token="{self.key}"',
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FunctionError("Jellyfin didn't accept the API key (the Jellyfin settings)") from exc
            raise FunctionError(f"Jellyfin answered {exc.code} for {path}") from exc
        except OSError as exc:
            raise FunctionError(f"can't reach Jellyfin at {self.url}: {getattr(exc, 'reason', exc)}") from exc
        return json.loads(body) if body else None

    def post(self, path: str, data: dict[str, Any]) -> None:
        req = urllib.request.Request(f"{self.url}{path}", data=json.dumps(data).encode(), method="POST",
                                     headers={"Authorization": f'MediaBrowser Token="{self.key}"',
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except OSError:
            pass                                  # progress reports are best-effort

    def user_id(self) -> str:
        if self._uid:
            return self._uid
        users = self.get("/Users") or []
        if not users:
            raise FunctionError("Jellyfin has no users")
        want = normalize(self.user_name)
        pick = next((u for u in users if normalize(u.get("Name", "")) == want), None) if want else None
        if pick is None and want:
            raise FunctionError(f"Jellyfin has no user called {self.user_name}")
        if pick is None:                          # no user named: the administrator (usually you), else the first
            pick = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), users[0])
        self._uid = pick["Id"]
        return self._uid

    # ------------------------------------------------------------------ finding
    def search(self, query: str, kind: str = "any") -> list[dict[str, Any]]:
        res = self.get("/Items", userId=self.user_id(), searchTerm=query, Recursive="true", Limit=25,
                       IncludeItemTypes=",".join(KINDS.get(kind, KINDS["any"])),
                       Fields="UserData,Path,Container,ProductionYear,SeriesName,Chapters,MediaSources")
        return (res or {}).get("Items") or []

    def find(self, query: str, kind: str = "any") -> dict[str, Any]:
        items = self.search(query, kind)
        if not items and kind != "any":
            items = self.search(query, "any")
        if not items:
            raise FunctionError(f"Jellyfin has nothing called {query}")
        want = normalize(query)

        def score(it: dict[str, Any]) -> float:
            name = normalize(it.get("Name", ""))
            s = similarity(want, name)
            if want == name:
                s += 1.0
            elif want and (want in name or name in want):
                s += 0.4
            if it.get("Type") == "Episode":         # a show asked for by name: the show, not one episode of it
                s -= 0.3
            return s
        return max(items, key=score)

    def episode(self, series: dict[str, Any], season: int | None, number: int | None) -> dict[str, Any]:
        uid = self.user_id()
        if season is None and number is None:
            nxt = (self.get("/Shows/NextUp", userId=uid, seriesId=series["Id"], Limit=1,
                            Fields="UserData,Chapters") or {}).get("Items") or []
            if nxt:
                return nxt[0]
        eps = (self.get(f"/Shows/{series['Id']}/Episodes", userId=uid, Fields="UserData,Chapters",
                        season=season if season is not None else None) or {}).get("Items") or []
        if not eps:
            raise FunctionError(f"{series.get('Name')} has no episodes" +
                                (f" in season {season}" if season is not None else ""))
        if number is not None:
            hit = next((e for e in eps if e.get("IndexNumber") == number), None)
            if hit is None:
                raise FunctionError(f"there's no episode {number}" + (f" in season {season}" if season is not None
                                                                      else ""))
            return hit
        return eps[0]

    # ------------------------------------------------------------------ playing / opening
    def stream_url(self, item: dict[str, Any]) -> str:
        # the file as it is ("static"): the player (FFmpeg) handles mkv, mp4, flac ... without transcoding
        return f"{self.url}/Videos/{item['Id']}/stream?static=true&api_key={urllib.parse.quote(self.key)}" \
            if item.get("MediaType") != "Audio" else \
            f"{self.url}/Audio/{item['Id']}/stream?static=true&api_key={urllib.parse.quote(self.key)}"

    def resume_ms(self, item: dict[str, Any]) -> int:
        ticks = int(((item.get("UserData") or {}).get("PlaybackPositionTicks")) or 0)
        return ticks // 10_000

    def chapters(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"start": int(c.get("StartPositionTicks", 0)) / TICKS, "title": c.get("Name") or ""}
                for c in item.get("Chapters") or []]

    def web_page(self, item: dict[str, Any]) -> str:
        return f"{self.url}/web/#/details?id={item['Id']}"

    def download(self, item: dict[str, Any], folder: str) -> str:
        """A book's file, kept in folder (downloaded once)."""
        ext = (item.get("Container") or os.path.splitext(item.get("Path") or "")[1].lstrip(".") or "epub").split(",")[0]
        safe = re.sub(r"[^\w .,'()-]+", "_", item.get("Name") or item["Id"]).strip() or item["Id"]
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{safe}.{ext}")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        req = urllib.request.Request(f"{self.url}/Items/{item['Id']}/Download",
                                     headers={"Authorization": f'MediaBrowser Token="{self.key}"'})
        tmp = path + ".part"
        try:
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
                while chunk := r.read(1 << 16):
                    out.write(chunk)
        except OSError as exc:
            raise FunctionError(f"couldn't download {item.get('Name')} from Jellyfin: {exc}") from exc
        os.replace(tmp, path)
        return path

    def report(self, item: dict[str, Any], position_ms: int, stopped: bool = False, started: bool = False) -> None:
        """Playback reports, the way Jellyfin's own players send them (start, progress, stopped), so its
        "continue watching", Next Up and watched marks follow what you watched in Jeeves' player."""
        body = {"ItemId": item["Id"], "PositionTicks": int(position_ms) * 10_000, "CanSeek": True,
                "PlaySessionId": item.get("_session") or f"jeeves-{item['Id']}", "PlayMethod": "DirectStream"}
        path = "/Sessions/Playing" if started else "/Sessions/Playing/Stopped" if stopped else \
            "/Sessions/Playing/Progress"
        self.post(path, body)
        if stopped:                               # and the position itself, in case the session was lost
            ticks = int(position_ms) * 10_000
            run = int(item.get("RunTimeTicks") or 0)
            if run and ticks >= run * 0.92:       # (near the end: watched)
                self.post(f"/UserPlayedItems/{item['Id']}?userId={self.user_id()}", {})
            else:
                self.post(f"/UserItems/{item['Id']}/UserData?userId={self.user_id()}",
                          {"PlaybackPositionTicks": ticks})


NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                       "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else NUMBERS.get(word.lower())


def parse_request(text: str) -> dict[str, Any]:
    """"put on The Office season 2 episode 5 on jellyfin" -> {title, kind, season, episode, action}."""
    t = re.sub(r"\s+", " ", re.sub(r"[,;:!?\"“”]+", " ", text)).strip(" .")
    t = re.sub(r"\s*\b(?:on|from|in)\s+(?:my\s+)?jellyfin\b", "", t, flags=re.I).strip()
    kind = "any"
    m = re.match(r"^(?:(?:can you\s+)?(?:play|put on|start|watch|open|read|listen to|pull up|resume|continue))\s+(.+)$",
                 t, flags=re.I)
    verb = (t.split()[0] if t else "").lower()
    if m:
        t = m.group(1)
    if verb == "read":
        kind = "book"
    elif verb == "listen":
        kind = "audiobook"
    m = re.match(r"^(?:the\s+)?(movie|film|show|series|book|audiobook|audio book|album|song)\s+(.+)$", t, flags=re.I)
    if m:
        kind = {"film": "movie", "series": "show", "audio book": "audiobook", "album": "music", "song": "music"}.get(
            m.group(1).lower(), m.group(1).lower())
        t = m.group(2)
    m2 = re.search(r"\s+(?:the\s+)?(movie|film|show|series|book|audiobook)$", t, flags=re.I)
    if m2:
        kind = {"film": "movie", "series": "show"}.get(m2.group(1).lower(), m2.group(1).lower())
        t = t[:m2.start()]
    season = episode = None
    m = re.search(r"\s*\b(?:season|series)\s+(\w+)(?:\s*,?\s*episode\s+(\w+))?\s*$", t, flags=re.I) or \
        re.search(r"\s*\bs(\d+)\s*e(\d+)\s*$", t, flags=re.I)
    if m:
        season, episode = _num(m.group(1)), (_num(m.group(2)) if m.group(2) else None)
        t, kind = t[:m.start()], "show"
    else:
        m = re.search(r"\s*\bepisode\s+(\w+)\s*$", t, flags=re.I)
        if m:
            episode, t, kind = _num(m.group(1)), t[:m.start()], "show"
    t = re.sub(r"^(?:the\s+next\s+episode\s+of|next\s+episode\s+of|the\s+next\s+one\s+of)\s+", "", t, flags=re.I)
    out: dict[str, Any] = {"title": t.strip(" .'"), "kind": kind}
    if season is not None:
        out["season"] = season
    if episode is not None:
        out["episode"] = episode
    return out


def label(item: dict[str, Any]) -> str:
    if item.get("Type") == "Episode":
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        where = f"season {s} episode {e}" if s is not None and e is not None else ""
        return f"{item.get('SeriesName') or ''}, {where}: {item.get('Name')}".strip(", :")
    year = item.get("ProductionYear")
    return f"{item.get('Name')}" + (f" ({year})" if year and item.get("Type") == "Movie" else "")


def from_settings(settings: Any) -> Jellyfin:
    from ...config import get_secret          # the key lives in secrets.json (mode 600), not the settings
    key = get_secret("jellyfin_api_key") or str(settings.get("jellyfin.api_key", "") or "")
    return Jellyfin(str(settings.get("jellyfin.url", "") or ""), key, str(settings.get("jellyfin.user", "") or ""))
