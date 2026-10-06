"""YouTube through yt-dlp: search, a channel's newest uploads, and stream URLs for the
Jeeves video player (overlay popups process).

    "the newest video from moist critikal" -> find the channel (search, match the
        channel name), list its uploads (newest first), take the first
    "play lofi hip hop" -> search, best match
    "the newest moist critikal video about elden ring" -> channel uploads whose title
        matches the topic

Streams: up to 1080p as separate video + audio (the player keeps them in sync),
falling back to a single combined stream.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from typing import Any

from ...util import normalize, similarity, which
from ..base import Arg, FunctionError, partial

FORMAT = ("bv*[height<=?{h}][vcodec~='^(avc1|h264)']+ba[ext=m4a]/bv*[height<=?{h}]+ba/"
          "b[height<=?{h}]/b")


def _ytdlp(*args: str, timeout: float = 60) -> Any:
    exe = which("yt-dlp")
    if not exe:
        raise FunctionError("yt-dlp isn't installed (needed for YouTube)")
    try:
        out = subprocess.run([exe, "--no-warnings", "-J", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FunctionError("YouTube took too long to answer") from exc
    if out.returncode != 0:
        err = (out.stderr or "").strip().splitlines()
        raise FunctionError(f"YouTube: {err[-1] if err else 'yt-dlp failed'}")
    try:
        return json.loads(out.stdout)
    except ValueError as exc:
        raise FunctionError("YouTube sent something unexpected") from exc


def _video(e: dict[str, Any]) -> dict[str, Any]:
    vid = e.get("id", "")
    return {"id": vid, "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader") or "",
            "channel_id": e.get("channel_id") or "", "duration": e.get("duration"),
            "url": e.get("url") if str(e.get("url", "")).startswith("http") else f"https://www.youtube.com/watch?v={vid}"}


def search(query: str, count: int = 8) -> list[dict[str, Any]]:
    data = _ytdlp("--flat-playlist", f"ytsearch{count}:{query}")
    return [_video(e) for e in data.get("entries") or [] if e.get("id")]


def channel_uploads(channel_id: str, count: int = 15) -> list[dict[str, Any]]:
    """A channel's videos, newest first."""
    url = channel_id if channel_id.startswith("http") else f"https://www.youtube.com/channel/{channel_id}/videos"
    data = _ytdlp("--flat-playlist", "--playlist-end", str(count), url)
    entries = data.get("entries") or []
    if entries and entries[0].get("_type") == "playlist":       # channel page with tabs
        entries = entries[0].get("entries") or []
    vids = [_video(e) for e in entries if e.get("id")]
    for v in vids:
        v["channel"] = v["channel"] or data.get("channel") or data.get("uploader") or ""
    return vids


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(s))


def _name_score(want: str, name: str) -> float:
    w, n = _squash(want), _squash(name)
    if not w or not n:
        return 0.0
    if w == n:
        return 1.0
    return max(similarity(w, n), 0.9 if (w in n or n in w) and min(len(w), len(n)) >= 4 else 0.0)


def _try(*args: str, timeout: float = 30) -> Any:
    try:
        return _ytdlp(*args, timeout=timeout)
    except Exception:  # noqa: BLE001 -- one way of finding it failing is fine; the next is tried
        return None


def find_channel(name: str) -> tuple[str, str]:
    """(channel id or URL, channel name) for a spoken channel name ("moist critikal" -> Moist Critikal),
    the exact channel -- not just whoever shows up first in a search:
      1. its @handle (most channels' handle is their name without spaces),
      2. YouTube's channel search (only channels),
      3. the channels of matching videos."""
    best: tuple[float, str, str] = (0.0, "", "")
    handle = _squash(name)
    if handle:
        data = _try("--flat-playlist", "--playlist-end", "1", f"https://www.youtube.com/@{handle}/videos")
        if data:
            ch = data.get("channel") or data.get("uploader") or data.get("title", "").removesuffix(" - Videos")
            cid = data.get("channel_id") or ""
            if cid and _name_score(name, ch) >= 0.6:
                return cid, ch
    data = _try("--flat-playlist", "--playlist-end", "8",
                "https://www.youtube.com/results?search_query=" + urllib.parse.quote(name) + "&sp=EgIQAg%3D%3D")
    for e in (data or {}).get("entries") or []:
        ch = e.get("channel") or e.get("title") or e.get("uploader") or ""
        cid = e.get("channel_id") or (e.get("id") if str(e.get("id", "")).startswith("UC") else "")
        score = _name_score(name, ch)
        if cid and score > best[0]:
            best = (score, cid, ch)
    if best[0] >= 0.85:
        return best[1], best[2]
    for v in search(name, 10):
        if not v.get("channel_id"):
            continue
        score = _name_score(name, v.get("channel", ""))
        if score > best[0]:
            best = (score, v["channel_id"], v.get("channel", ""))
    if best[0] < 0.6:
        raise FunctionError(f"I couldn't find a YouTube channel called {name}")
    return best[1], best[2]


STOPWORDS = set("the a an and or of to in on for with that this which who is are was video videos vid one "
                "into all about from by his her their my your".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", normalize(text)) if w not in STOPWORDS and len(w) > 1}


def match_score(query: str, v: dict[str, Any]) -> float:
    """How well a video fits a description: shared words with its title (stems count: "theories" ~
    "theory"), a bonus when the channel is named in the description, a penalty for Shorts."""
    q = _words(query)
    if not q:
        return 0.0
    t = _words(v.get("title", ""))
    hits = sum(1 for w in q if w in t or any(len(w) > 4 and len(x) > 4 and (w[:5] == x[:5]) for x in t))
    score = hits / len(q)
    ch = _squash(v.get("channel", ""))
    if ch and len(ch) >= 4 and ch in _squash(query):
        score += 0.6
    if (v.get("duration") or 999) < 61:
        score -= 0.2
    return score


def channel_search(channel_id: str, query: str, count: int = 10) -> list[dict[str, Any]]:
    """Search inside one channel."""
    base = channel_id if channel_id.startswith("http") else f"https://www.youtube.com/channel/{channel_id}"
    data = _try("--flat-playlist", "--playlist-end", str(count),
                base.rstrip("/") + "/search?query=" + urllib.parse.quote(query))
    return [_video(e) for e in (data or {}).get("entries") or [] if e.get("id")]


def pick(query: str = "", channel: str = "", newest: bool = False) -> dict[str, Any]:
    """The video meant by a (possibly vague) request."""
    guessed = False
    full = query
    if not channel and query:
        channel, query = channel_in(query)
        guessed = bool(channel)
    if channel:
        try:
            cid, cname = find_channel(channel)
        except FunctionError:
            if not query:
                raise
            cid, cname, query = "", "", full if guessed else f"{channel} {query}"
        if cid and guessed:
            # "the alex bale video that..." -- only trust the guessed channel if it has a matching video
            found = channel_search(cid, query) + channel_uploads(cid)
            best = max(found, key=lambda v: match_score(query, v), default=None)
            if best is not None and match_score(query, best) >= 0.5:
                return best
            cid, query = "", full
        if cid:
            vids = channel_uploads(cid)
            if not vids:
                raise FunctionError(f"{cname} has no videos I can see")
            if not query:
                return vids[0]                   # uploads are newest first
            scored = sorted(((match_score(query, v), -i, v) for i, v in enumerate(vids)), key=lambda x: x[:2],
                            reverse=True)
            if scored and scored[0][0] >= 0.5:
                return scored[0][2]
            found = channel_search(cid, query) or \
                [v for v in search(f"{cname} {query}", 10) if v.get("channel_id") == cid]
            if found:
                return max(found, key=lambda v: match_score(query, v))
            if newest:
                return vids[0]
            if scored and scored[0][0] > 0:
                return scored[0][2]
            raise FunctionError(f"I couldn't find a {cname} video about {query}")
    if not query:
        raise FunctionError("which video?")
    results = search(query, 15)
    if not results:
        raise FunctionError(f"I couldn't find a video for {query}")
    ranked = sorted(enumerate(results), key=lambda iv: (match_score(query, iv[1]), -iv[0]), reverse=True)
    return ranked[0][1]


def channel_in(query: str) -> tuple[str, str]:
    """("alex bale", "spongebob conspiracy theories") from "alex bale video that combines all the spongebob
    conspiracy theories" -- a channel named before the word "video". ("", query) when there's none."""
    m = re.match(r"^(?:the\s+|a\s+)?(.+?)(?:'s)?\s+(?:video|vid|upload)s?\s+(?:that|which|where|about|on|with|of|"
                 r"called|named|when)\s+(.+)$", query.strip(), re.I)
    if m and len(m.group(1).split()) <= 4 and not re.match(r"^(?:newest|latest|new|last|most recent|that|this)$",
                                                             m.group(1), re.I):
        return m.group(1).strip(), m.group(2).strip()
    return "", query


def streams(url: str, max_height: int = 1080) -> dict[str, Any]:
    """{title, channel, video, audio (None = combined), duration, page} for the player."""
    info = _ytdlp("--no-playlist", "-f", FORMAT.format(h=max_height), url, timeout=90)
    heights = sorted({int(f["height"]) for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none") and int(f["height"]) >= 144},
                     reverse=True)
    req = info.get("requested_formats")
    if req and len(req) >= 2:
        video = next((f["url"] for f in req if f.get("vcodec") not in (None, "none")), req[0]["url"])
        audio = next((f["url"] for f in req if f.get("acodec") not in (None, "none") and f["url"] != video), None)
    else:
        video, audio = info.get("url"), None
    if not video:
        raise FunctionError("YouTube didn't give a playable stream for that video")
    req = req or [info]
    vfmt = next((f for f in req if f.get("vcodec") not in (None, "none")), req[0])
    chapters = [{"start": float(c.get("start_time") or 0), "end": float(c.get("end_time") or 0),
                 "title": c.get("title") or ""} for c in info.get("chapters") or []]
    return {"title": info.get("title", ""), "channel": info.get("channel") or info.get("uploader") or "",
            "video": video, "audio": audio, "duration": info.get("duration"), "page": info.get("webpage_url", url),
            "height": vfmt.get("height") or info.get("height"), "fps": vfmt.get("fps") or info.get("fps") or 30,
            "heights": heights, "chapters": chapters}


def parse_request(text: str) -> dict[str, Any]:
    """'the newest video from moist critikal' -> {channel, newest}; 'play lofi on youtube' -> {query}."""
    t = re.sub(r"^\W*(?:can you\s+|could you\s+|please\s+)?(?:pull up|put on|play|show me|find|open|watch)\s+",
               "", text.strip(), flags=re.I)
    t = re.sub(r"\s+(?:on|from)\s+youtube\b", "", t, flags=re.I).strip(" .?!")
    newest = bool(re.search(r"\b(newest|latest|most recent|new|last)\b", t, re.I))
    m = re.search(r"^(?:the\s+)?(?:newest|latest|most recent|new|last)?\s*(?:video|upload|vid)s?\s+(?:from|by|of)\s+"
                  r"(.+?)(?:\s+(?:about|on)\s+(.+))?$", t, re.I)
    if m:
        return {"channel": m.group(1).strip(), "query": (m.group(2) or "").strip(), "newest": newest}
    m = re.search(r"^(?:the\s+)?(?:newest|latest|most recent|new|last)\s+(.+?)\s+(?:video|upload|vid)"
                  r"(?:\s+(?:about|on)\s+(.+))?$", t, re.I)
    if m:
        return {"channel": m.group(1).strip(), "query": (m.group(2) or "").strip(), "newest": True}
    m = re.search(r"^(.+?)\s+(?:by|from)\s+(.+)$", t, re.I)
    if m and not re.search(r"\b(video|youtube)\b", m.group(1), re.I):
        return {"query": m.group(1).strip(), "channel": m.group(2).strip(), "newest": newest}
    t = re.sub(r"^(?:a|the|some)\s+(?:youtube\s+)?(?:video|vid)s?\s+(?:of|about|on)\s+", "", t, flags=re.I)
    ch, rest = channel_in(t)
    if ch:                                     # "the alex bale video that combines ..."
        return {"query": rest, "channel": ch, "newest": newest}
    return {"query": re.sub(r"\s+", " ", re.sub(r"\b(?:youtube|video)\b", "", t, flags=re.I)).strip(),
            "channel": "", "newest": newest}


@partial("youtube_search", "Searches YouTube (or a channel's newest uploads) and returns matching videos.",
         args=[Arg("query", "string", "What to search for", required=False, default=""),
               Arg("channel", "string", "Only this channel's uploads (newest first)", required=False, default="")],
         returns="list of {title, channel, url, duration}", category="web", dry_run_safe=True)
def youtube_search(ctx, query="", channel=""):
    if channel:
        cid, _ = find_channel(str(channel))
        vids = channel_uploads(cid)
        if query:
            q = set(normalize(str(query)).split())
            vids = [v for v in vids if q & set(normalize(v["title"]).split())] or vids
        return vids
    return search(str(query))
