"""Jellyfin: finding and playing shows, movies and books from a (fake) server."""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ITEMS = [
    {"Id": "s1", "Name": "The Office", "Type": "Series"},
    {"Id": "m1", "Name": "Dune", "Type": "Movie", "ProductionYear": 2021, "RunTimeTicks": 93_000_000_000,
     "UserData": {"PlaybackPositionTicks": 12_000_000_000}, "Chapters": [{"StartPositionTicks": 0, "Name": "Start"}]},
    {"Id": "m2", "Name": "Dune: Part Two", "Type": "Movie", "ProductionYear": 2024},
    {"Id": "b1", "Name": "Project Hail Mary", "Type": "Book", "Container": "epub"},
]
EPISODES = [{"Id": f"e{s}{e}", "Name": f"Ep {s}.{e}", "Type": "Episode", "SeriesName": "The Office",
             "ParentIndexNumber": s, "IndexNumber": e} for s in (1, 2) for e in (1, 2, 3)]
SEEN = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        SEEN.append(("GET", u.path, q, self.headers.get("Authorization")))
        if 'Token="KEY"' not in (self.headers.get("Authorization") or ""):
            self.send_response(401)
            self.end_headers()
            return
        if u.path == "/Users":
            return self._send([{"Id": "u1", "Name": "mrtw", "Policy": {"IsAdministrator": True}}])
        if u.path == "/Items":
            kinds = q.get("IncludeItemTypes", "").split(",")
            term = q.get("searchTerm", "").lower()
            return self._send({"Items": [i for i in ITEMS if i["Type"] in kinds and term.split()[0] in i["Name"].lower()]})
        if u.path == "/Shows/NextUp":
            return self._send({"Items": [EPISODES[4]]})
        if u.path == "/Shows/s1/Episodes":
            s = q.get("season")
            return self._send({"Items": [e for e in EPISODES if s is None or e["ParentIndexNumber"] == int(s)]})
        if u.path == "/Items/b1/Download":
            return self._send(None, raw=b"EPUBDATA")
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        SEEN.append(("POST", self.path, json.loads(self.rfile.read(n) or b"{}"), None))
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    SEEN.clear()
    yield f"127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _setup(engine, url, monkeypatch):
    engine.settings.set("jellyfin.url", url)
    monkeypatch.setenv("JEEVES_JELLYFIN_API_KEY", "KEY")


def _plays(engine):
    ev = []
    engine._publish_fn = lambda t, d: ev.append((t, d))
    return ev


def test_parse_requests():
    from jeeves.functions.partials.jellyfin import parse_request as p
    assert p("put on The Office season two episode 5 on jellyfin") == \
        {"title": "The Office", "kind": "show", "season": 2, "episode": 5}
    assert p("play the movie Dune on Jellyfin") == {"title": "Dune", "kind": "movie"}
    assert p("read Project Hail Mary from jellyfin") == {"title": "Project Hail Mary", "kind": "book"}
    assert p("play Breaking Bad S03E07 on jellyfin")["season"] == 3


def test_a_movie_plays_from_where_you_left_off(engine, server, monkeypatch):
    _setup(engine, server, monkeypatch)
    ev = _plays(engine)
    res = engine.handle_text("Jeeves, play the movie Dune on Jellyfin", wait=True)
    video = [d for t, d in ev if t == "video"][-1]
    assert video["title"] == "Dune (2021)" and video["start"] == 1_200_000 and video["report"]
    assert "/Videos/m1/stream?static=true&api_key=KEY" in video["video"]
    assert "Dune (2021), from 20 minutes in" in str(res)
    deadline = time.time() + 3
    while time.time() < deadline and not any(s[1] == "/Sessions/Playing" for s in SEEN):
        time.sleep(0.02)
    assert any(s[1] == "/Sessions/Playing" for s in SEEN)
    engine.video_closed(9_000_000)                       # closed near the end (2.5 h of 2.6): watched
    deadline = time.time() + 3
    while time.time() < deadline and not any("/UserPlayedItems/m1" in s[1] for s in SEEN):
        time.sleep(0.02)
    assert any("/UserPlayedItems/m1" in s[1] for s in SEEN)


def test_a_show_plays_next_up_or_the_episode_named(engine, server, monkeypatch):
    _setup(engine, server, monkeypatch)
    ev = _plays(engine)
    engine.handle_text("Jeeves, put on The Office on Jellyfin", wait=True)
    assert [d for t, d in ev if t == "video"][-1]["title"] == "The Office, season 2 episode 2: Ep 2.2"
    engine.handle_text("Jeeves, put on The Office season 1 episode 3 on Jellyfin", wait=True)
    assert [d for t, d in ev if t == "video"][-1]["title"] == "The Office, season 1 episode 3: Ep 1.3"


def test_a_book_is_downloaded_and_opened(engine, server, monkeypatch):
    import subprocess
    _setup(engine, server, monkeypatch)
    opened = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **k: opened.append(argv))
    engine.handle_text("Jeeves, read Project Hail Mary from Jellyfin", wait=True)
    assert opened and opened[-1][0] == "xdg-open" and opened[-1][1].endswith("Project Hail Mary.epub")
    with open(opened[-1][1], "rb") as f:
        assert f.read() == b"EPUBDATA"


def test_not_set_up_says_where(engine):
    res = engine.handle_text("Jeeves, play Dune on Jellyfin", wait=True)
    assert "Jellyfin isn't set up" in json.dumps(res)
