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


def find_channel(name: str) -> tuple[str, str]:
    """(channel id, channel name) for a spoken channel name ("moist critikal" -> penguinz0 / Moist Critikal)."""
    want = _squash(name)
    best: tuple[float, str, str] = (0.0, "", "")
    for v in search(name, 10):
        ch = v.get("channel", "")
        if not v.get("channel_id"):
            continue
        sq = _squash(ch)
        score = 1.0 if sq == want else max(similarity(want, sq), 0.9 if want and (want in sq or sq in want) else 0)
        if score > best[0]:
            best = (score, v["channel_id"], ch)
    if best[0] < 0.6:
        raise FunctionError(f"I couldn't find a YouTube channel called {name}")
    return best[1], best[2]


def pick(query: str = "", channel: str = "", newest: bool = False) -> dict[str, Any]:
    """The video meant by a (possibly vague) request."""
    if channel:
        cid, cname = find_channel(channel)
        vids = channel_uploads(cid)
        if not vids:
            raise FunctionError(f"{cname} has no videos I can see")
        if query:
            q = set(normalize(query).split())
            scored = [(len(q & set(normalize(v["title"]).split())), -i, v) for i, v in enumerate(vids)]
            top = max(scored)
            if top[0] > 0:
                return top[2]
            if not newest:
                found = [v for v in search(f"{cname} {query}", 5) if v.get("channel_id") == cid]
                if found:
                    return found[0]
        return vids[0]                       # uploads are newest first
    if not query:
        raise FunctionError("which video?")
    results = search(query, 8)
    if not results:
        raise FunctionError(f"I couldn't find a video for {query}")
    return results[0]


def streams(url: str, max_height: int = 1080) -> dict[str, Any]:
    """{title, channel, video, audio (None = combined), duration, page} for the player."""
    info = _ytdlp("--no-playlist", "-f", FORMAT.format(h=max_height), url, timeout=90)
    req = info.get("requested_formats")
    if req and len(req) >= 2:
        video = next((f["url"] for f in req if f.get("vcodec") not in (None, "none")), req[0]["url"])
        audio = next((f["url"] for f in req if f.get("acodec") not in (None, "none") and f["url"] != video), None)
    else:
        video, audio = info.get("url"), None
    if not video:
        raise FunctionError("YouTube didn't give a playable stream for that video")
    return {"title": info.get("title", ""), "channel": info.get("channel") or info.get("uploader") or "",
            "video": video, "audio": audio, "duration": info.get("duration"), "page": info.get("webpage_url", url)}


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
    return {"query": re.sub(r"\b(?:youtube|video)\b", "", t, flags=re.I).strip(), "channel": "", "newest": newest}


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
