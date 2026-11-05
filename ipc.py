"""Daemon <-> client protocol: newline-delimited JSON over a Unix socket.

Request:   {"id": 7, "method": "settings.set", "params": {...}}
Response:  {"id": 7, "result": ...}      or   {"id": 7, "error": "message"}
Event:     {"event": "state", "data": {...}}   (only after a "subscribe")

Clients here are blocking and stdlib-only so the CLI starts instantly and the
GUI can run one on a worker thread.
"""
from __future__ import annotations

import itertools
import json
import socket
import threading
from typing import Any, Callable

from . import paths


class DaemonError(RuntimeError):
    pass


class DaemonUnavailable(DaemonError):
    pass


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, default=str) + "\n").encode("utf-8")


class Client:
    """One blocking request/response connection."""

    def __init__(self, path: str | None = None, timeout: float = 10.0) -> None:
        self.path = path or str(paths.socket_path())
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.path)
            except OSError as exc:
                s.close()
                raise DaemonUnavailable(f"jeeves daemon isn't running ({exc}); start it with "
                                        "`systemctl --user start jeeves` or `jeeves daemon`") from exc
            self._sock = s
        return self._sock

    def _readline(self) -> dict[str, Any]:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise DaemonUnavailable("daemon closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def call(self, method: str, timeout: float | None = None, **params: Any) -> Any:
        with self._lock:
            s = self._connect()
            if timeout is not None:
                s.settimeout(timeout)
            rid = next(self._ids)
            try:
                s.sendall(encode({"id": rid, "method": method, "params": params}))
                while True:
                    msg = self._readline()
                    if msg.get("id") == rid:
                        break
            except (OSError, ValueError) as exc:
                self.close()
                raise DaemonUnavailable(str(exc)) from exc
            finally:
                if timeout is not None and self._sock is not None:
                    self._sock.settimeout(self.timeout)
        if "error" in msg:
            raise DaemonError(msg["error"])
        return msg.get("result")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""


class Subscriber:
    """A connection that receives pushed events on a background thread."""

    def __init__(self, topics: list[str], on_event: Callable[[str, Any], None],
                 on_disconnect: Callable[[], None] | None = None, path: str | None = None) -> None:
        self.topics = topics
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.path = path or str(paths.socket_path())
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="jeeves-subscriber")

    def start(self) -> "Subscriber":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _run(self) -> None:
        # reconnect forever: the daemon may restart underneath the GUI/overlay
        while not self._stop.is_set():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.path)
                self._sock = s
                s.sendall(encode({"id": 0, "method": "subscribe", "params": {"topics": self.topics}}))
                buf = b""
                while not self._stop.is_set():
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line.decode("utf-8"))
                        except ValueError:
                            continue
                        if "event" in msg:
                            try:
                                self.on_event(msg["event"], msg.get("data"))
                            except Exception:  # never let a handler kill the reader
                                import traceback
                                traceback.print_exc()
            except OSError:
                pass
            finally:
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None
            if self.on_disconnect and not self._stop.is_set():
                self.on_disconnect()
            self._stop.wait(1.0)


def call(method: str, **params: Any) -> Any:
    """One-shot convenience."""
    c = Client()
    try:
        return c.call(method, **params)
    finally:
        c.close()
