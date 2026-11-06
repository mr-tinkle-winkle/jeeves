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
    {"Id": "p1", "Name": "Primetime", "Type": "Series"},
]
EPISODES = [{"Id": f"e{s}{e}", "Name": f"Ep {s}.{e}", "Type": "Episode", "SeriesName": "The Office",
             "ParentIndexNumber": s, "IndexNumber": e} for s in (1, 2) for e in (1, 2, 3)]
SEEN = []
APPS = []


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
        if u.path == "/Users/Me":
            return self._send({"Id": "u1", "Name": "mrtw"})
        if u.path == "/Sessions":
            return self._send(APPS)
        if u.path == "/Users":
            return self._send([{"Id": "u1", "Name": "mrtw", "Policy": {"IsAdministrator": True}}])
        if u.path == "/Items":
            kinds = q.get("IncludeItemTypes", "").split(",")
            term = q.get("searchTerm", "").lower()
            # (like Jellyfin's: every word of the search in the name)
            return self._send({"Items": [i for i in ITEMS if i["Type"] in kinds and
                                         all(w in i["Name"].lower() for w in term.split())]})
        if u.path == "/Shows/NextUp" and q.get("seriesId") == "s1":
            return self._send({"Items": [EPISODES[4]]})
        if u.path == "/Shows/s1/Episodes":
            s = q.get("season")
            return self._send({"Items": [e for e in EPISODES if s is None or e["ParentIndexNumber"] == int(s)]})
        if u.path.startswith("/Users/u1/Items/"):
            return self._send(next(i for i in ITEMS if i["Id"] == u.path.rsplit("/", 1)[1]))
        if u.path == "/Shows/p1/Episodes" or (u.path == "/Shows/NextUp" and q.get("seriesId") == "p1"):
            return self._send({"Items": [{"Id": "pe1", "Name": "Pilot", "Type": "Episode", "SeriesName": "Primetime",
                                          "ParentIndexNumber": 1, "IndexNumber": 1}]})
        if u.path == "/Items/b1/Download":
            return self._send(None, raw=b"EPUBDATA")
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if self.path == "/Users/AuthenticateByName":
            body = json.loads(self.rfile.read(n))
            ok = body == {"Username": "mrtw", "Pw": "pw"}
            self.send_response(200 if ok else 401)
            self.end_headers()
            if ok:
                self.wfile.write(json.dumps({"AccessToken": "KEY", "User": {"Id": "u1", "Name": "mrtw"}}).encode())
            return
        SEEN.append(("POST", self.path, json.loads(self.rfile.read(n) or b"{}"), None))
        self.send_response(204)
        self.end_headers()


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    SEEN.clear()
    APPS.clear()
    yield f"127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _setup(engine, url, monkeypatch, open_in="player"):
    engine.settings.set("jellyfin.url", url)
    engine.settings.set("jellyfin.open_in", open_in)
    monkeypatch.setenv("JEEVES_JELLYFIN_TOKEN", "KEY")


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


def test_signing_in_needs_no_api_key(server):
    from jeeves.functions.base import FunctionError
    from jeeves.functions.partials.jellyfin import sign_in
    assert sign_in(server, "mrtw", "pw") == {"token": "KEY", "user_id": "u1", "user": "mrtw"}
    with pytest.raises(FunctionError):
        sign_in(server, "mrtw", "wrong")


def test_it_plays_in_your_jellyfin_app(engine, server, monkeypatch):
    _setup(engine, server, monkeypatch, open_in="app")
    APPS.append({"Id": "sess1", "Client": "Jellyfin Media Player", "DeviceName": "x", "SupportsRemoteControl": True,
                 "UserId": "u1"})
    res = engine.handle_text("Jeeves, play the movie Dune on Jellyfin", wait=True)
    play = [x for x in SEEN if x[0] == "POST" and x[1].startswith("/Sessions/sess1/Playing")]
    assert play and "itemIds=m1" in play[0][1] and "startPositionTicks=12000000000" in play[0][1]
    assert "in Jellyfin, from 20 minutes in" in str(res)


def test_no_app_falls_back_to_the_player(engine, server, monkeypatch):
    from jeeves.functions.partials import jellyfin as jf
    _setup(engine, server, monkeypatch, open_in="app")
    monkeypatch.setattr(jf, "app_command", lambda settings: None)
    ev = _plays(engine)
    engine.handle_text("Jeeves, play the movie Dune on Jellyfin", wait=True)
    assert [d for t, d in ev if t == "video"]


def test_spelling_that_doesnt_match_the_title_still_finds_it(engine, server, monkeypatch):
    _setup(engine, server, monkeypatch)
    ev = _plays(engine)
    engine.handle_text("Jeeves, put on prime time on Jellyfin", wait=True)
    assert [d for t, d in ev if t == "video"][-1]["title"] == "Primetime, season 1 episode 1: Pilot"


def test_the_model_picks_from_the_library_when_nothing_is_close(engine, server, monkeypatch):
    _setup(engine, server, monkeypatch)
    ev = _plays(engine)
    asked = []
    monkeypatch.setattr(engine.models, "respond", lambda agent, prompt, **k: asked.append(prompt) or "The Office")
    engine.handle_text("Jeeves, put on that paper company show on Jellyfin", wait=True)
    assert asked and "Primetime" in asked[0]
    assert [d for t, d in ev if t == "video"][-1]["title"].startswith("The Office")
