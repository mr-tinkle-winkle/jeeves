"""Which models are loaded, for whom, and when they are unloaded.

* Each kind (stt, intent, tts, local_response) has a global model choice and
  agents can override it.
* Instances are shared by model id (intent and local response on the same
  model use one llama-server).
* "Unload when these apps are open": a watcher checks every few seconds.
  While a kind is suspended, using it raises ``ModelUnavailable`` -- the
  indicator flashes red and the request is queued until the app closes and
  the model is back.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from ..daemon import desktop as dk
from . import catalog
from .backends import LLM, STT, TTS, BackendError, VoskWake, make_llm, make_stt, make_tts
from .download import install, is_installed, uninstall

log = logging.getLogger("jeeves.models")

KINDS = ("stt", "intent", "tts", "local_response")


def runtime_available(engine: str) -> bool:
    """Is the program/library that runs this kind of model installed?"""
    import importlib.util
    import shutil
    if engine in ("vosk", "kokoro"):
        return importlib.util.find_spec("vosk" if engine == "vosk" else "kokoro_onnx") is not None
    exe = {"whisper.cpp": ("whisper-server", "whisper-cpp-server"), "llama.cpp": ("llama-server",),
           "piper": ("piper",), "espeak-ng": ("espeak-ng", "espeak")}.get(engine)
    return exe is None or any(shutil.which(x) for x in exe)


class ModelUnavailable(RuntimeError):
    def __init__(self, kind: str, reason: str, queueable: bool = False) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.queueable = queueable


class ModelManager:
    def __init__(self, settings: Any, publish: Callable[[str, Any], None],
                 on_available: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self.publish = publish
        self.on_available = on_available
        self._lock = threading.RLock()
        self.instances: dict[str, Any] = {}           # model id -> backend
        self.suspended: dict[str, str] = {}           # kind -> app that caused it
        self.downloads: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self.wake: VoskWake | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True, name="jeeves-model-watch")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for inst in self.instances.values():
                try:
                    inst.unload()
                except Exception:
                    pass
            self.instances.clear()

    # ---- choice ----------------------------------------------------------
    def model_id(self, kind: str, agent: dict[str, Any] | None = None) -> str | None:
        if agent:
            override = (agent.get("models") or {}).get(kind)
            if override:
                return override
        if kind == "tts_voice":
            return self.settings.get("models.tts_voice")
        return self.settings.get(f"models.{kind}.model")

    def _check(self, kind: str, model_id: str | None) -> catalog.ModelEntry:
        if kind in self.suspended:
            raise ModelUnavailable(kind, f"{kind.replace('_', ' ')} model is unloaded while "
                                         f"{self.suspended[kind]} is open", queueable=True)
        entry = catalog.get(model_id)
        if entry is None:
            raise ModelUnavailable(kind, f"no {kind.replace('_', ' ')} model is chosen (Settings > Models)")
        if not is_installed(entry):
            raise ModelUnavailable(kind, f"{entry.name} isn't downloaded yet (Settings > Models)")
        return entry

    def _instance(self, entry: catalog.ModelEntry, factory: Callable[[], Any]) -> Any:
        with self._lock:
            inst = self.instances.get(entry.id)
            if inst is None:
                inst = factory()
                self.instances[entry.id] = inst
            return inst

    def _loaded(self, inst: Any, kind: str) -> Any:
        if not inst.loaded():
            self.publish("models", {"loading": kind})
            try:
                inst.load()
            except BackendError as exc:
                raise ModelUnavailable(kind, str(exc)) from exc
            finally:
                self.publish("models", self.status())
        return inst

    def stt(self, agent: dict[str, Any] | None = None) -> STT:
        entry = self._check("stt", self.model_id("stt", agent))
        threads = int(self.settings.get("models.threads", 0))
        return self._loaded(self._instance(entry, lambda: make_stt(entry, threads)), "stt")

    def llm(self, kind: str, agent: dict[str, Any] | None = None) -> LLM:
        entry = self._check(kind, self.model_id(kind, agent))
        threads = int(self.settings.get("models.threads", 0))
        ngl = int(self.settings.get("models.gpu_layers", 0))
        reasoning = self.settings.get("models.reasoning", "off")
        return self._loaded(self._instance(entry, lambda: make_llm(entry, threads, ngl, reasoning)), kind)

    def tts(self, agent: dict[str, Any] | None = None) -> tuple[TTS, catalog.ModelEntry | None]:
        entry = self._check("tts", self.model_id("tts", agent))
        voice = catalog.get(self.model_id("tts_voice", agent))
        if voice is not None and voice.tts_model != entry.id:
            voice = None
        if voice is not None and not is_installed(voice):
            raise ModelUnavailable("tts", f"voice {voice.name} isn't downloaded yet")
        if voice is None:
            choices = [v for v in catalog.voices_for(entry.id) if is_installed(v)]
            voice = choices[0] if choices else None
        return self._loaded(self._instance(entry, lambda: make_tts(entry)), "tts"), voice

    def wake_spotter(self) -> VoskWake | None:
        model = self.settings.get("wake_word.model")
        entry = catalog.get(model)
        if entry is None or entry.engine != "vosk" or not is_installed(entry):
            return None
        with self._lock:
            if self.wake is None or self.wake.entry.id != entry.id:
                self.wake = VoskWake(entry)
            if not self.wake.loaded():
                try:
                    self.wake.load()
                except BackendError as exc:
                    log.warning("wake word model: %s", exc)
                    return None
            return self.wake

    # ---- conveniences used by functions ------------------------------------
    def respond(self, agent: dict[str, Any], prompt: str, system: str = "", ctx: Any = None,
                with_memory: bool = False, raw: bool = False) -> str | None:
        try:
            llm = self.llm("local_response", agent)
        except ModelUnavailable as exc:
            if ctx is not None:
                ctx.think(f"Local response model unavailable: {exc.reason}")
            if exc.queueable:
                raise
            return None
        messages: list[dict[str, str]] = []
        sys_parts = []
        if not raw and agent.get("prompt"):
            sys_parts.append(agent["prompt"])
        if system:
            sys_parts.append(system)
        if with_memory and ctx is not None:
            mem = ctx.engine.memory.context_for(ctx.agent_id)
            if mem:
                sys_parts.append(mem)
            recent = ctx.engine.history.recent(int(self.settings.get("memory.recent_count", 3)), exclude=ctx.request.get("id"))
            for r in recent:
                messages.append({"role": "user", "content": r["text"]})
                if r.get("result"):
                    messages.append({"role": "assistant", "content": str(r["result"])})
        if not raw:
            sys_parts.append("Reply in plain spoken English: your reply is read aloud, so no markdown, lists or "
                             "code unless asked. Keep it short unless asked for detail.")
        if sys_parts:
            messages.insert(0, {"role": "system", "content": "\n\n".join(sys_parts)})
        messages.append({"role": "user", "content": prompt})
        on_token = (lambda t: ctx.think(t, append=True)) if ctx is not None else None
        cancelled = ctx.is_cancelled if ctx is not None else None
        try:
            return llm.chat(messages, max_tokens=int(self.settings.get("models.local_response.max_tokens", 512)),
                            on_token=on_token, cancelled=cancelled).strip()
        except BackendError as exc:
            if ctx is not None:
                ctx.think(f"Model error: {exc}")
            return None

    def vision_locate(self, image_path: Any, target: str) -> dict[str, Any] | None:
        # A multimodal llama-server needs an --mmproj file; none of the catalog
        # models ship one, so object finding falls back to OCR text matching.
        return None

    # ---- downloads -------------------------------------------------------
    def download(self, model_id: str) -> None:
        entry = catalog.get(model_id)
        if entry is None:
            raise ValueError(f"unknown model '{model_id}'")
        if model_id in self._cancel:
            return
        cancel = threading.Event()
        self._cancel[model_id] = cancel

        def progress(p: dict[str, Any]) -> None:
            self.downloads[model_id] = dict(p, state="downloading")
            self.publish("download", self.downloads[model_id])

        def worker() -> None:
            try:
                if entry.tts_model and not entry.files:
                    parent = catalog.get(entry.tts_model)
                    if parent and not is_installed(parent):
                        install(parent, progress, cancel)
                else:
                    install(entry, progress, cancel)
                self.downloads[model_id] = {"model": model_id, "state": "done"}
            except Exception as exc:
                self.downloads[model_id] = {"model": model_id, "state": "error", "error": str(exc)}
            finally:
                self._cancel.pop(model_id, None)
                self.publish("download", self.downloads[model_id])
                self.publish("models", self.status())

        threading.Thread(target=worker, daemon=True, name=f"download-{model_id}").start()

    def cancel_download(self, model_id: str) -> None:
        ev = self._cancel.get(model_id)
        if ev:
            ev.set()

    def remove(self, model_id: str) -> None:
        entry = catalog.get(model_id)
        if entry is None:
            raise ValueError(f"unknown model '{model_id}'")
        with self._lock:
            inst = self.instances.pop(model_id, None)
            if inst:
                inst.unload()
        uninstall(entry)
        self.publish("models", self.status())

    def unload_all(self) -> None:
        with self._lock:
            for inst in self.instances.values():
                inst.unload()

    # ---- status ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._lock:
            loaded = {mid for mid, inst in self.instances.items() if inst.loaded()}
        kinds = {}
        for kind in KINDS:
            mid = self.model_id(kind)
            entry = catalog.get(mid)
            kinds[kind] = {"model": mid, "installed": bool(entry and is_installed(entry)),
                           "loaded": mid in loaded, "suspended_by": self.suspended.get(kind)}
        return {
            "kinds": kinds,
            "voice": self.model_id("tts_voice"),
            "catalog": [dict(m.to_dict(), installed=is_installed(m), runtime=runtime_available(m.engine))
                        for m in catalog.CATALOG],
            "downloads": self.downloads,
            "loaded": sorted(loaded),
        }

    # ---- app-based unloading ----------------------------------------------
    def _watch(self) -> None:
        while not self._stop.wait(3.0):
            try:
                self._apply_app_rules()
            except Exception:
                log.exception("model watcher failed")

    def _apply_app_rules(self) -> None:
        rules = {k: self.settings.get(f"models.{k}.unload_when_open", []) or [] for k in KINDS}
        if not any(rules.values()):
            if self.suspended:
                for kind in list(self.suspended):
                    self._resume(kind)
            return
        names = dk.open_apps() + dk.processes()
        for kind, patterns in rules.items():
            hit = next((p for p in patterns if dk.app_matches([p], names)), None)
            if hit and kind not in self.suspended:
                self._suspend(kind, hit)
            elif not hit and kind in self.suspended:
                self._resume(kind)

    def _ids_for_kind(self, kind: str) -> set[str]:
        ids = {self.model_id(kind)}
        for agent in (self.settings.get("agents", {}) or {}).values():
            ids.add(self.model_id(kind, agent))
        return {i for i in ids if i}

    def _suspend(self, kind: str, app: str) -> None:
        log.info("unloading %s model while %s is open", kind, app)
        self.suspended[kind] = app
        keep: set[str] = set()
        for other in KINDS:
            if other != kind and other not in self.suspended:
                keep |= self._ids_for_kind(other)
        with self._lock:
            for mid in self._ids_for_kind(kind) - keep:
                inst = self.instances.get(mid)
                if inst is not None and inst.loaded():
                    inst.unload()
        self.publish("models", self.status())

    def _resume(self, kind: str) -> None:
        log.info("%s model available again", kind)
        self.suspended.pop(kind, None)
        # reload the global choice in the background so the next request is fast
        try:
            if kind == "stt":
                self.stt()
            elif kind == "tts":
                self.tts()
            else:
                self.llm(kind)
        except ModelUnavailable:
            pass
        self.publish("models", self.status())
        if self.on_available:
            self.on_available(kind)

    def preload(self) -> None:
        """Load the global choices at startup (in the background)."""
        def go() -> None:
            for kind in ("stt", "intent"):
                try:
                    self.stt() if kind == "stt" else self.llm(kind)
                except ModelUnavailable as exc:
                    log.info("%s not loaded: %s", kind, exc.reason)
        threading.Thread(target=go, daemon=True, name="jeeves-preload").start()


__all__ = ["ModelManager", "ModelUnavailable"]
