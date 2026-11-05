"""A small local web server between the video player and YouTube.

The player (Qt's FFmpeg backend) opening YouTube's stream addresses itself fails with "Could not open
file" for several reasons it can't do anything about: YouTube refuses requests without the headers yt-dlp
was given for that stream (its User-Agent and so on), refuses or throttles a request for the whole file
instead of pieces, and some FFmpeg builds can't do TLS. So the player opens http://127.0.0.1:PORT/<token>
instead, and this server fetches from YouTube with the right headers, in pieces of at most CHUNK bytes,
passing on the player's seeking (Range requests).

Only the addresses registered here are served, each under a random token, and only on 127.0.0.1."""
from __future__ import annotations

import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHUNK = 10 * 1024 * 1024           # what yt-dlp uses for YouTube too
KEEP_S = 6 * 3600                  # YouTube's addresses expire after about six hours anyway

_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_streams: dict[str, dict[str, Any]] = {}


def _upstream(entry: dict[str, Any], start: int, end: int | None) -> Any:
    rng = f"bytes={start}-" + ("" if end is None else str(end))
    req = urllib.request.Request(entry["url"], headers={**entry["headers"], "Range": rng})
    return urllib.request.urlopen(req, timeout=30)


def _total(resp: Any) -> int | None:
    m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
    if m:
        return int(m.group(1))
    n = resp.headers.get("Content-Length")
    return int(n) if n and resp.status == 200 else None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a: Any) -> None:            # quiet
        pass

    def _entry(self) -> dict[str, Any] | None:
        with _lock:
            return _streams.get(self.path.strip("/").split("?")[0])

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        entry = self._entry()
        if entry is None:
            self.send_error(404)
            return
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        ranged = bool(m and (m.group(1) or m.group(2)))
        start = int(m.group(1)) if m and m.group(1) else 0
        want_end = int(m.group(2)) if m and m.group(2) else None
        if m and not m.group(1) and m.group(2):            # "bytes=-N": the last N bytes
            total = entry.get("total") or self._learn_total(entry)
            if total is None:
                self.send_error(416)
                return
            start, want_end = max(0, total - int(m.group(2))), total - 1
        first_end = start + CHUNK - 1 if want_end is None else min(want_end, start + CHUNK - 1)
        try:
            resp = _upstream(entry, start, first_end)
        except urllib.error.HTTPError as exc:
            if exc.code == 416:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{entry.get('total') or 0}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_error(502, f"YouTube said {exc.code}")
            return
        except OSError:
            self.send_error(502, "YouTube didn't answer")
            return
        total = _total(resp)
        if total is not None:
            entry["total"] = total
        end = (total - 1 if total is not None else None) if want_end is None else want_end
        if total is not None and end is not None:
            end = min(end, total - 1)
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", entry.get("type") or resp.headers.get("Content-Type") or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        if end is not None:
            self.send_header("Content-Length", str(end - start + 1))
            if ranged:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total if total is not None else '*'}")
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if not body:
            resp.close()
            return
        pos = start
        try:
            while True:
                with resp:
                    shutil.copyfileobj(resp, self.wfile, 256 * 1024)
                pos = first_end + 1
                if end is not None and pos > end:
                    break
                first_end = pos + CHUNK - 1 if end is None else min(end, pos + CHUNK - 1)
                try:
                    resp = _upstream(entry, pos, first_end)
                except urllib.error.HTTPError as exc:
                    if exc.code == 416:                  # past the end (length wasn't known)
                        break
                    raise
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True              # the player seeked away or closed: fine

    def _learn_total(self, entry: dict[str, Any]) -> int | None:
        try:
            with _upstream(entry, 0, 0) as r:
                entry["total"] = _total(r)
        except OSError:
            return None
        return entry["total"]


def _ensure() -> ThreadingHTTPServer:
    global _server
    with _lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, name="stream-proxy", daemon=True).start()
        return _server


def register(url: str, headers: dict[str, str] | None = None, content_type: str = "") -> str:
    """The local address the player should open for this stream."""
    srv = _ensure()
    token = secrets.token_urlsafe(12)
    now = time.time()
    with _lock:
        for k in [k for k, v in _streams.items() if now - v["at"] > KEEP_S]:
            del _streams[k]
        _streams[token] = {"url": url, "headers": {k: v for k, v in (headers or {}).items()
                                                   if k.lower() not in ("range", "host", "accept-encoding")},
                           "type": content_type, "at": now, "total": None}
    return f"http://127.0.0.1:{srv.server_address[1]}/{token}"


def stop() -> None:
    global _server
    with _lock:
        srv, _server = _server, None
        _streams.clear()
    if srv is not None:
        srv.shutdown()
        srv.server_close()
