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


def _base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    return url if re.match(r"https?://", url) or not url else "http://" + url


def _auth(token: str = "") -> str:
    import socket
    host = socket.gethostname() or "computer"
    return (f'MediaBrowser Client="Jeeves", Device="{host}", DeviceId="jeeves-{host}", Version="1.0"'
            + (f', Token="{token}"' if token else ""))


def sign_in(url: str, username: str, password: str, timeout: float = 15.0) -> dict[str, str]:
    """Sign in as a user of the server -- yours on a friend's server; no admin or API key needed.
    -> {"token", "user_id", "user"} (the token stays valid until signed out on the server)."""
    base = _base(url)
    if not base:
        raise FunctionError("add the Jellyfin server's address first")
    req = urllib.request.Request(f"{base}/Users/AuthenticateByName", method="POST",
                                 data=json.dumps({"Username": username, "Pw": password}).encode(),
                                 headers={"Authorization": _auth(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise FunctionError("Jellyfin didn't accept that username and password" if exc.code in (400, 401, 403)
                            else f"Jellyfin answered {exc.code}") from exc
    except OSError as exc:
        raise FunctionError(f"can't reach Jellyfin at {base}: {getattr(exc, 'reason', exc)}") from exc
    user = data.get("User") or {}
    return {"token": data.get("AccessToken", ""), "user_id": user.get("Id", ""), "user": user.get("Name", username)}


class Jellyfin:
    def __init__(self, url: str, token: str, user: str = "", user_id: str = "", timeout: float = 10.0) -> None:
        if not url or not token:
            raise FunctionError("Jellyfin isn't set up: sign in in the Jellyfin settings (Listening & keys)")
        self.url = _base(url)
        self.key, self.user_name, self.timeout = token, user, timeout
        self._uid = user_id

    # ------------------------------------------------------------------ HTTP
    def get(self, path: str, **params: Any) -> Any:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, doseq=True)
        req = urllib.request.Request(f"{self.url}{path}{'?' + q if q else ''}",
                                     headers={"Authorization": _auth(self.key), "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FunctionError("Jellyfin signed Jeeves out: sign in again in the Jellyfin settings") from exc
            raise FunctionError(f"Jellyfin answered {exc.code} for {path}") from exc
        except OSError as exc:
            raise FunctionError(f"can't reach Jellyfin at {self.url}: {getattr(exc, 'reason', exc)}") from exc
        return json.loads(body) if body else None

    def post(self, path: str, data: dict[str, Any] | None = None, quiet: bool = True) -> bool:
        req = urllib.request.Request(f"{self.url}{path}", data=json.dumps(data or {}).encode(), method="POST",
                                     headers={"Authorization": _auth(self.key), "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()
            return True
        except OSError as exc:
            if not quiet:
                raise FunctionError(f"Jellyfin refused {path.split('?')[0]}: {exc}") from exc
            return False                          # progress reports are best-effort

    def user_id(self) -> str:
        if self._uid:
            return self._uid
        try:                                      # a user's own token: who it is
            me = self.get("/Users/Me")
            if me and me.get("Id"):
                self._uid = me["Id"]
                return self._uid
        except FunctionError:
            pass
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

    def find(self, query: str, kind: str = "any", guess: Any = None) -> dict[str, Any]:
        """The item asked for, trying harder each step:
          1. Jellyfin's search for what was said;
          2. looser searches -- words joined or split ("prime time" / "primetime"), each long word alone --
             judged by how the names compare with spaces and punctuation ignored, and by sound;
          3. the whole library's names, compared the same way;
          4. guess(asked, names) -> a name: the language model picks from the library."""
        want = normalize(query)
        items = self.search(query, kind) or (self.search(query, "any") if kind != "any" else [])
        best = self._best(items, want)
        if best is not None and self.closeness(want, best.get("Name", "")) >= 0.9:
            return best
        tried = {query.lower()}
        loose: list[dict[str, Any]] = list(items)
        for q in self.variants(query):
            if q.lower() in tried:
                continue
            tried.add(q.lower())
            loose += self.search(q, kind)
        best = self._best(loose, want)
        if best is not None and self.closeness(want, best.get("Name", "")) >= 0.78:
            return best
        library = self.everything(kind)
        best = self._best(library, want)
        if best is not None and self.closeness(want, best.get("Name", "")) >= 0.78:
            return best
        if guess is not None and library:
            names = list(dict.fromkeys(i.get("Name", "") for i in library if i.get("Name")))
            picked = guess(query, names)
            if picked:
                hit = [i for i in library if normalize(i.get("Name", "")) == normalize(picked)]
                if hit:
                    return self._full(hit[0])
        if items:                                    # nothing close: what Jellyfin's own search liked best
            return self._best(items, want)
        raise FunctionError(f"Jellyfin has nothing called {query}")

    @staticmethod
    def closeness(want: str, name: str) -> float:
        from .youtube import sound_key
        a, b = normalize(want), normalize(name)
        sa, sb = re.sub(r"[^a-z0-9]", "", a), re.sub(r"[^a-z0-9]", "", b)
        b_short = re.sub(r"^(?:the|a|an) ", "", b)
        s = max(similarity(a, b), similarity(a, b_short), similarity(sa, sb))
        if sa and sa == sb:
            s = 1.0                                  # "prime time" = "Primetime" = "Prime-Time"
        elif len(sa) >= 4 and (sa in sb or sb in sa):
            s = max(s, 0.8)
        ka, kb = sound_key(a), sound_key(b)
        if len(ka) >= 4 and ka == kb:
            s = max(s, 0.85)
        return s

    def _best(self, items: list[dict[str, Any]], want: str) -> dict[str, Any] | None:
        if not items:
            return None

        def score(it: dict[str, Any]) -> float:
            s = self.closeness(want, it.get("Name", ""))
            if it.get("Type") == "Episode":         # a show asked for by name: the show, not one episode of it
                s -= 0.3
            return s
        return max(items, key=score)

    @staticmethod
    def variants(query: str) -> list[str]:
        words = normalize(query).split()
        out = ["".join(words)] if len(words) > 1 else []
        w = words[0] if words else ""
        if len(words) == 1 and len(w) >= 6:          # "primetime" -> "prime time" (every split point)
            out += [f"{w[:i]} {w[i:]}" for i in range(3, len(w) - 2)]
        out += [x for x in words if len(x) >= 4 and x not in ("the", "show", "movie", "with", "from")]
        out += [x[:4] for x in words if len(x) >= 6]
        return list(dict.fromkeys(out))[:12]

    def everything(self, kind: str = "any") -> list[dict[str, Any]]:
        """Every show / movie / book in the library: names only (cached a few minutes)."""
        import time
        key = kind
        cache = self.__dict__.setdefault("_library", {})
        if key in cache and time.time() - cache[key][0] < 300:
            return cache[key][1]
        types = [t for t in KINDS.get(kind, KINDS["any"]) if t not in ("Episode", "Audio")] or KINDS.get(kind)
        res = self.get("/Items", userId=self.user_id(), Recursive="true", IncludeItemTypes=",".join(types),
                       Fields="ProductionYear", EnableImages="false", EnableUserData="false", Limit=20000)
        items = (res or {}).get("Items") or []
        cache[key] = (time.time(), items)
        return items

    def _full(self, item: dict[str, Any]) -> dict[str, Any]:
        """An item from the bare library list, with what playing it needs."""
        got = self.get(f"/Users/{self.user_id()}/Items/{item['Id']}")
        return got or item

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

    # ------------------------------------------------------------------ the Jellyfin app
    def app_session(self) -> dict[str, Any] | None:
        """A Jellyfin app of yours that can be remote-controlled (Jellyfin Media Player / Desktop, the web
        app in a browser), preferring one on this computer, then the most recently used."""
        import socket
        host = (socket.gethostname() or "").lower()
        sessions = self.get("/Sessions", controllableByUserId=self.user_id()) or []
        apps = [s for s in sessions if s.get("SupportsRemoteControl") and s.get("Client") != "Jeeves"
                and s.get("UserId") in (None, "", self.user_id())]
        if not apps:
            return None
        return max(apps, key=lambda s: ((s.get("DeviceName") or "").lower() == host,
                                        "media player" in (s.get("Client") or "").lower() or
                                        "desktop" in (s.get("Client") or "").lower(),
                                        s.get("LastActivityDate") or ""))

    def play_in_app(self, session: dict[str, Any], item: dict[str, Any]) -> None:
        """The app plays it -- from where you left off -- as if you'd picked it there."""
        q = urllib.parse.urlencode({"playCommand": "PlayNow", "itemIds": item["Id"],
                                    "startPositionTicks": int(((item.get("UserData") or {}).get(
                                        "PlaybackPositionTicks")) or 0)})
        self.post(f"/Sessions/{session['Id']}/Playing?{q}", quiet=False)

    def show_in_app(self, session: dict[str, Any], item: dict[str, Any]) -> None:
        """The app opens its page (a book: where its reader is)."""
        q = urllib.parse.urlencode({"itemType": item.get("Type", ""), "itemId": item["Id"],
                                    "itemName": item.get("Name", "")})
        self.post(f"/Sessions/{session['Id']}/Viewing?{q}", quiet=False)

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


APP_COMMANDS = [["jellyfin-desktop"], ["jellyfinmediaplayer"], ["jellyfin-media-player"],
                ["flatpak", "run", "org.jellyfin.JellyfinDesktop"],
                ["flatpak", "run", "com.github.iwalton3.jellyfin-media-player"]]


def app_command(settings: Any) -> list[str] | None:
    import shlex
    import shutil
    own = str(settings.get("jellyfin.app_command", "") or "").strip()
    if own:
        return shlex.split(own)
    for cmd in APP_COMMANDS:
        if shutil.which(cmd[0]) and (cmd[0] != "flatpak" or _flatpak_has(cmd[2])):
            return cmd
    return None


def _flatpak_has(app: str) -> bool:
    import subprocess
    try:
        return subprocess.run(["flatpak", "info", app], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def from_settings(settings: Any) -> Jellyfin:
    from ...config import get_secret          # the sign-in lives in secrets.json (mode 600), not the settings
    token = get_secret("jellyfin_token") or get_secret("jellyfin_api_key") or ""
    return Jellyfin(str(settings.get("jellyfin.url", "") or ""), token, str(settings.get("jellyfin.user", "") or ""),
                    str(settings.get("jellyfin.user_id", "") or ""))
