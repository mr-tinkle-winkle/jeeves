"""The daemon process: a Unix-socket JSON-line server in front of the Engine.

The GUI, the overlay and the CLI are all clients. Nothing but the daemon
changes settings or runs functions -- clients send requests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from typing import Any, Callable

from .. import config, paths
from ..models import voicefx
from ..ipc import encode
from ..util import which
from . import desktop as dk
from . import doctor
from .engine import Engine

log = logging.getLogger("jeeves.server")


class Server:
    def __init__(self, start_io: bool = True) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: dict[asyncio.StreamWriter, set[str]] = {}
        self.engine = Engine(publish=self.publish, start_io=start_io)
        self.methods: dict[str, Callable[..., Any]] = self._methods()

    # ---- pub/sub ---------------------------------------------------------
    def publish(self, topic: str, data: Any) -> None:
        if self.loop is None:
            return
        msg = encode({"event": topic, "data": data})
        self.loop.call_soon_threadsafe(self._fanout, topic, msg)

    def _fanout(self, topic: str, msg: bytes) -> None:
        for w, topics in list(self.subscribers.items()):
            if "*" in topics or topic in topics:
                try:
                    w.write(msg)
                except (ConnectionError, RuntimeError):
                    self.subscribers.pop(w, None)

    # ---- methods ---------------------------------------------------------
    def _methods(self) -> dict[str, Callable[..., Any]]:
        e = self.engine
        s = e.settings

        def _agent(a: Any) -> dict[str, Any]:
            """An agent as edited in the GUI (a dict), or one by its id (from the command line)."""
            if isinstance(a, str):
                if a not in e.agents():
                    raise ValueError(f"no agent '{a}'")
                return dict(e.agents()[a], id=a)
            return a

        def settings_get(path: str = "") -> Any:
            return {"value": s.get(path) if path else s.effective(), "locked": sorted(s.locked_paths()),
                    "user": s.user()}

        def settings_set(changes: dict[str, Any]) -> Any:
            s.set_many(changes)
            e.apply_settings()
            self.publish("settings", {"changed": list(changes)})
            return True

        def settings_reset(path: str) -> Any:
            s.reset(path)
            e.apply_settings()
            self.publish("settings", {"changed": [path]})
            return True

        def agent_save(id: str, agent: dict[str, Any]) -> Any:
            if not id or not id.replace("_", "").replace("-", "").isalnum():
                raise ValueError("agent id must be letters, digits, - or _")
            s.set(f"agents.{id}", agent)
            e.apply_settings()
            self.publish("settings", {"changed": [f"agents.{id}"]})
            return True

        def agent_delete(id: str) -> Any:
            if id in config.DEFAULTS["agents"]:
                s.set(f"agents.{id}", {"deleted": True})
            else:
                s.delete(f"agents.{id}")
            self.publish("settings", {"changed": [f"agents.{id}"]})
            return True

        def functions_dictionary(agent: str | None = None) -> Any:
            a = e.agents().get(agent or "") if agent else None
            funcs = e.registry.enabled_for(a) if a else e.registry.all()
            return e.registry.dictionary_text(funcs, e.history.rated_examples())

        def functions_save(function: dict[str, Any]) -> Any:
            f = e.registry.save_user_function(function)
            self.publish("functions", {"changed": f.name})
            return f.to_dict()

        def functions_delete(name: str) -> Any:
            e.registry.delete_user_function(name)
            self.publish("functions", {"deleted": name})
            return True

        def functions_validate(steps: Any) -> Any:
            from ..functions.composer import validate
            return validate(steps, set(e.registry.functions))

        def functions_reload() -> Any:
            e.registry.reload()
            self.publish("functions", {"reloaded": True})
            return {"count": len(e.registry.functions), "problems": e.registry.problems}

        def accounts_set_key(name: str, value: str | None) -> Any:
            if name not in ("gemini_api_key", "jellyfin_api_key"):
                raise ValueError("unknown secret")
            config.save_secret(name, value)
            return True

        def jellyfin_sign_in(url: str, username: str, password: str) -> Any:
            from ..functions.partials import jellyfin
            got = jellyfin.sign_in(url, username, password)      # (the password itself isn't kept)
            config.save_secret("jellyfin_token", got["token"])
            e.settings.set("jellyfin.url", url.strip())
            e.settings.set("jellyfin.user", got["user"])
            e.settings.set("jellyfin.user_id", got["user_id"])
            return {"user": got["user"]}

        def jellyfin_status() -> Any:
            from ..functions.partials import jellyfin
            try:
                j = jellyfin.from_settings(e.settings)
                me = j.get("/Users/Me") or {}
                app = j.app_session()
                return {"ok": True, "user": me.get("Name", ""), "app": (app or {}).get("Client", ""),
                        "app_command": " ".join(jellyfin.app_command(e.settings) or [])}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "problem": str(exc)}

        def jellyfin_sign_out() -> Any:
            config.save_secret("jellyfin_token", None)
            e.settings.set("jellyfin.user_id", "")
            return True

        def audio_devices() -> Any:
            out = {"sources": [], "sinks": []}
            if which("pactl"):
                for kind in ("sources", "sinks"):
                    try:
                        text = subprocess.run(["pactl", "list", "short", kind], capture_output=True, text=True,
                                              timeout=3).stdout
                        out[kind] = [ln.split("\t")[1] for ln in text.splitlines() if "\t" in ln]
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            from .audio import list_devices, list_outputs
            out["devices"] = list_devices()
            out["outputs"] = list_outputs()
            return out

        def keyboard_call(what: str, **kw: Any) -> Any:
            """The Record buttons next to key combos, and the keyboard's state (Listening & Keys)."""
            kb = e.keyboard
            if kb is None:
                return {"recording": False, "keys": [], "held": [], "status": "off"}
            if what == "record":
                return kb.record(float(kw.get("seconds") or 10))
            if what == "recorded":
                return kb.recorded()
            return {"status": kb.status, "held": kb.held(), "hold_keys": e.hold_keys()}

        def wiki_search(query: str) -> Any:
            return e.wikipedia.lookup(query, 2000)

        return {
            "ping": lambda: "pong",
            "status": e.status,
            "settings.get": settings_get,
            "settings.set": settings_set,
            "settings.reset": settings_reset,
            "settings.defaults": lambda: config.DEFAULTS,
            "agents.list": e.agents,
            "agents.save": agent_save,
            "agents.delete": agent_delete,
            "agents.lobotomize": lambda id: e.lobotomize(id),
            "agents.default": lambda name="New agent": config.default_agent(name),
            "functions.list": e.registry.to_json,
            "functions.dictionary": functions_dictionary,
            "functions.save": functions_save,
            "functions.delete": functions_delete,
            "functions.validate": functions_validate,
            "functions.reload": functions_reload,
            "functions.export": lambda names=None, app="jeeves-export": e.registry.export(names, app),
            "functions.import": lambda manifest: e.imports.submit(manifest, origin="socket"),
            "functions.pending": lambda: list(e.imports.pending.values()),
            "functions.decide": lambda id, approve=None, deny_all=False: e.imports.decide(id, approve, deny_all),
            "request.text": lambda text, agent=None: _strip_entry(e.handle_text(text, agent, source="text")),
            "request.voice": lambda agent=None: {"id": e.voice_request(agent)},
            "debug.speak": lambda agent=None, text="": e.speak_test(agent, text),
            "request.dry_run": lambda text, agent=None: e.dry_run(text, agent),
            "request.answer": lambda request=None, text=None, confirm=None: e.answer(request, text, confirm),
            "request.thoughts": lambda request: e.thoughts(request),
            "indicator.click": lambda request: e.indicator_click(request),
            "request.suspend": lambda request: e.suspend(request),
            "request.close": lambda request: e.close_request(request),
            "request.interrupt": lambda request: e.interrupt(request),
            "indicator.state": lambda: list(e.indicators.values()),
            "mic": lambda action: e.mic(action),
            "abort": e.abort,
            "power.get": e.is_on,
            "power.set": lambda on: e.set_enabled(bool(on)),
            "power.toggle": e.toggle,
            "history.list": lambda offset=0, limit=50, agent=None: e.history.list(offset, limit, agent),
            "history.get": lambda id: e.history.get(id),
            "history.rate": lambda id, rating, comment="", should_use=None: e.history.rate(id, rating, comment, should_use),
            "models.status": e.models.status,
            "models.recommend": e.models.recommend,
            "models.download": lambda id: e.models.download(id),
            "models.cancel": lambda id: e.models.cancel_download(id),
            "models.remove": lambda id: e.models.remove(id),
            "models.unload_all": e.models.unload_all,
            "accounts.status": e.online.status,
            "accounts.login": lambda name: e.online.login(name),
            "accounts.set_key": accounts_set_key,
            "jellyfin.sign_in": jellyfin_sign_in,
            "jellyfin.status": jellyfin_status,
            "jellyfin.sign_out": jellyfin_sign_out,
            "wikipedia.status": e.wikipedia.status,
            "wikipedia.update": e.wikipedia.update,
            "wikipedia.cancel": e.wikipedia.cancel,
            "wikipedia.search": wiki_search,
            "memory.list": e.memory.all,
            "memory.add": lambda text, permanent=False: e.memory.add(text, permanent),
            "memory.forget": lambda query: e.memory.forget(query),
            "timers.list": e.timers.list,
            "timers.cancel": lambda label=None: e.timers.cancel(label),
            "triggers.list": e.triggers.all,
            "triggers.add": lambda spec, agent=None, request="": e.triggers.add(spec, agent, request),
            "triggers.remove": lambda id: e.triggers.remove(id),
            "training.phrases": e.training.phrases,
            "wake_clips.list": e.clips.list,
            "wake_clips.mark": lambda id, good, said="": e.clips.mark(id, bool(good), said),
            "training.record": lambda text: e.training_record(text),
            "training.recordings": e.training.recordings,
            "training.evaluate": lambda: e.training.evaluate(e.models.stt()),
            "training.export": lambda dest: e.training.export_dataset(dest),
            "training.intent_list": e.training.intent_phrases,
            "training.intent_add": lambda text, function, args=None, agent=None: e.training.add_intent_phrase(
                text, function, args, agent),
            "training.intent_remove": lambda id: e.training.remove_intent_phrase(id),
            "ui.review": lambda: self.publish("show_review", {}),
            "ui.text_request": lambda: self.publish("show_text_request", {}),
            "ui.popup": lambda kind, data=None: self.publish("popup", {"kind": kind, "data": data or {}}),
            "puppetry.macros": e.puppetry.macro_names,
            "desktop.apps": lambda: {"open": dk.open_apps(), "processes": dk.processes()[:400]},
            "audio.devices": audio_devices,
            "debug.cpu": lambda seconds=30: __import__("jeeves.daemon.cpuwatch", fromlist=["profile"]).profile(seconds),
            "keyboard.record": lambda seconds=10: keyboard_call("record", seconds=seconds),
            "keyboard.recorded": lambda: keyboard_call("recorded"),
            "keyboard.state": lambda: keyboard_call("state"),
            "audio.test_virtual_mic": e.test_virtual_mic,
            "voice.preview": lambda agent, text="": e.preview_voice(_agent(agent), text),
            "persona.test": lambda agent: e.models.test_persona(_agent(agent)),
            "video.closed": lambda position=None: e.video_closed(position),
            "video.position": lambda position=0: e.video_position(position),
            "video.quality": lambda height, position=0: e.video_quality(height, position),
            "voice.speakers": lambda voice: e.voice_speakers(voice),
            "voice.effects": lambda: {"effects": [[k, v[0]] for k, v in voicefx.EFFECTS.items()],
                                      "sox": voicefx.available()},
            "summary.text": lambda minutes=None: e.summary.text(minutes),
            "summary.clear": e.summary.clear,
            "doctor": lambda move_test=True: doctor.overlay_checks(e) + doctor.run(e.control, bool(move_test), s,
                                                                                    e.registry),
        }

    # ---- connection handling --------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    writer.write(encode({"id": None, "error": "malformed request"}))
                    continue
                rid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
                if method == "subscribe":
                    self.subscribers[writer] = set(params.get("topics") or ["*"])
                    writer.write(encode({"id": rid, "result": True}))
                    await writer.drain()
                    continue
                if method == "ping":                    # answered here: never waits behind busy workers
                    writer.write(encode({"id": rid, "result": "pong"}))
                    await writer.drain()
                    continue
                fn = self.methods.get(method)
                if fn is None:
                    writer.write(encode({"id": rid, "error": f"unknown method '{method}'"}))
                    await writer.drain()
                    continue
                try:
                    result = await asyncio.get_running_loop().run_in_executor(None, lambda: fn(**params))
                    writer.write(encode({"id": rid, "result": result}))
                except config.LockedError as exc:
                    writer.write(encode({"id": rid, "error": str(exc), "locked": True}))
                except (ValueError, KeyError, TypeError, PermissionError, RuntimeError) as exc:
                    writer.write(encode({"id": rid, "error": f"{exc}" if str(exc) else type(exc).__name__}))
                except Exception as exc:
                    log.exception("method %s failed", method)
                    writer.write(encode({"id": rid, "error": f"{type(exc).__name__}: {exc}"}))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.subscribers.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        paths.ensure_all()
        sock = paths.socket_path()
        if sock.exists():
            # refuse to run twice; remove a stale socket
            try:
                r, w = await asyncio.open_unix_connection(str(sock))
                w.close()
                raise SystemExit("jeeves daemon is already running")
            except (ConnectionError, OSError):
                sock.unlink(missing_ok=True)
        if not self.engine.is_on():
            # switched off: don't run at all (logging in with Jeeves off keeps it off)
            log.info("Jeeves is switched off; not starting (turn it on with `jeeves on` or the GUI)")
            self.engine.stop()
            return
        server = await asyncio.start_unix_server(self.handle, path=str(sock), limit=16 * 1024 * 1024)
        os.chmod(sock, 0o600)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, stop.set)
        loop = self.loop
        self.engine.on_exit = lambda: loop.call_soon_threadsafe(stop.set)
        self.engine.start()
        from .. import __build__
        log.info("jeeves daemon (build %s) listening on %s", __build__, sock)
        # Everything loaded at startup lives as long as the daemon: out of the garbage collector's way
        # (its full sweeps went over all of it every few seconds -- the audio makes a lot of garbage).
        import gc
        gc.collect()
        gc.freeze()
        loop.call_later(90, lambda: (gc.collect(), gc.freeze()))     # (and what the model preload made)
        async with server:
            await stop.wait()
        log.info("stopping")
        # Stopping used to hang on a rebuild (systemd waits 90 s, then kills it): a request still
        # running, a model server still starting (its lock), and Python joining every worker thread
        # on the way out. Whatever is left after a few seconds isn't worth waiting for.
        _exit_after(STOP_SECONDS)
        sock.unlink(missing_ok=True)
        try:
            await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, self.engine.stop), STOP_SECONDS - 1)
        except asyncio.TimeoutError:
            log.warning("still stopping after %.0f s: leaving the rest", STOP_SECONDS - 1)
        log.info("stopped")
        _exit_now()


STOP_SECONDS = 6.0


def _exit_now(code: int = 0) -> None:
    """Out, without waiting on worker threads (concurrent.futures joins them all at exit, and one
    blocked in a model server or a long request holds the whole process up)."""
    import sys
    try:
        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


def _exit_after(seconds: float) -> None:
    import threading

    def go() -> None:
        log.warning("didn't stop within %.0f s: exiting anyway", seconds)
        _exit_now()
    t = threading.Timer(seconds, go)
    t.daemon = True
    t.start()


def _strip_entry(res: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in res.items() if k != "entry"}


def main(start_io: bool = True) -> None:
    import faulthandler
    import sys
    faulthandler.enable(sys.stderr, all_threads=True)    # native crashes leave a traceback in the journal
    logging.basicConfig(level=os.environ.get("JEEVES_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Server(start_io=start_io).serve())


if __name__ == "__main__":
    main()
