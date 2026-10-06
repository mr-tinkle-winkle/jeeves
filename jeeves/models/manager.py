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
import os
import threading
from typing import Any, Callable

from ..daemon import desktop as dk
from . import backends, catalog, hardware, persona
from .backends import LLM, STT, TTS, BackendError, VoskWake, make_llm, make_stt, make_tts
from .download import install, is_installed, uninstall

log = logging.getLogger("jeeves.models")

KINDS = ("stt", "intent", "tts", "local_response", "vision")


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
        self.gpu_offload: dict[str, dict[str, Any]] = {}     # model id -> last Auto GPU layers decision
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

    def enabled(self) -> bool:
        return bool(self.settings.get("general.enabled", True))

    def _check(self, kind: str, model_id: str | None) -> catalog.ModelEntry:
        if not self.enabled():
            raise ModelUnavailable(kind, "Jeeves is turned off")
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

    def keep_free(self) -> dict[str, float]:
        return hardware.keep_free(self.settings)

    def budget(self) -> dict[str, Any]:
        """The hardware minus Minimum untouched: what the AIs may use."""
        return hardware.budget(self.hardware(), self.keep_free())

    def _threads(self) -> int:
        """CPU threads per model server, never more than Minimum untouched CPU leaves.
        Also pins the servers to the cores that are left, so the kept ones stay idle."""
        keep = int(self.keep_free()["cpu_threads"])
        try:
            cores = sorted(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cores = list(range(os.cpu_count() or 4))
        allowed = cores[:max(1, len(cores) - keep)]
        backends.CPU_SET = set(allowed) if keep > 0 else None
        setting = int(self.settings.get("models.threads", 0) or 0)
        return min(setting, len(allowed)) if setting > 0 else len(allowed)

    def _check_ram(self, entry: catalog.ModelEntry, need_mb: float | None = None) -> None:
        """Refuse to load a model that would eat into Minimum untouched RAM."""
        avail = hardware.available_ram_mb()
        if avail is None:
            return
        keep = int(self.keep_free()["ram_gb"] * 1024)
        need = int(entry.ram_mb if need_mb is None else need_mb)
        if avail - need < keep:
            raise BackendError(f"not enough free RAM for {entry.name}: it needs about {need / 1024:.1f} GB, "
                               f"{avail / 1024:.1f} GB is free and {keep / 1024:.1f} GB must stay untouched "
                               "(Models > Minimum untouched). Pick a smaller model or close something.")

    def _gpu_layers_for(self, entry: catalog.ModelEntry) -> Any:
        """The -ngl value for llama-server: a function that, when the model actually loads,
        measures free VRAM (Auto) or takes the hand-set number, applies Minimum untouched
        VRAM/GPU, and checks that the part left on the CPU fits in the RAM allowed."""
        setting = self.settings.get("models.gpu_layers", "auto")
        fixed = None
        if setting != "auto":
            try:
                fixed = int(setting)
            except (TypeError, ValueError):
                fixed = 0

        def resolve(model_path: Any, ctx_size: int) -> int:
            keep = self.keep_free()
            n, info = hardware.auto_gpu_layers(model_path, ctx_size, self.hardware(),
                                               keep_vram_mb=int(keep["vram_gb"] * 1024),
                                               gpu_share=1.0 - keep["gpu_percent"] / 100.0, fixed=fixed)
            self.gpu_offload[entry.id] = dict(info, model=entry.name)
            log.info("GPU layers for %s: %s (%s)", entry.id, n, info.get("why"))
            self.publish("models", self.status())
            of = info.get("of")
            if of and info.get("model_mb"):
                on_gpu = min(1.0, info.get("layers", 0) / of)
                self._check_ram(entry, info["model_mb"] * (1 - on_gpu) + info.get("kv_mb", 0) * (1 - on_gpu) + 300)
            elif not n:
                self._check_ram(entry)
            return n
        return resolve

    def stt(self, agent: dict[str, Any] | None = None) -> STT:
        entry = self._check("stt", self.model_id("stt", agent))
        threads = self._threads()
        gpu = bool(self.budget().get("whisper_gpu"))
        inst = self._instance(entry, lambda: make_stt(entry, threads, gpu))
        if hasattr(inst, "threads"):
            inst.threads = threads             # Minimum untouched applies on the next load
        if hasattr(inst, "gpu"):
            inst.gpu = gpu
        if not inst.loaded():
            try:
                self._check_ram(entry)
            except BackendError as exc:
                raise ModelUnavailable("stt", str(exc)) from exc
        return self._loaded(inst, "stt")

    def llm(self, kind: str, agent: dict[str, Any] | None = None) -> LLM:
        entry = self._check(kind, self.model_id(kind, agent))
        threads = self._threads()
        ngl = self._gpu_layers_for(entry)
        reasoning = self.settings.get("models.reasoning", "off")
        inst = self._instance(entry, lambda: make_llm(entry, threads, ngl, reasoning))
        if hasattr(inst, "gpu_layers"):
            inst.threads, inst.gpu_layers = threads, ngl
        return self._loaded(inst, kind)

    def tts(self, agent: dict[str, Any] | None = None) -> tuple[TTS, catalog.ModelEntry | None]:
        voice = catalog.get(self.model_id("tts_voice", agent))
        engine_id = self.model_id("tts", agent)
        if voice is not None and voice.tts_model and voice.tts_model != engine_id:
            # picking a voice picks its engine (a Kokoro voice while the engine says Piper, ...)
            parent = catalog.get(voice.tts_model)
            if parent is not None and is_installed(parent):
                engine_id = parent.id
        entry = self._check("tts", engine_id)
        if voice is not None and voice.tts_model != entry.id:
            voice = None
        if voice is not None and not is_installed(voice):
            raise ModelUnavailable("tts", f"voice {voice.name} isn't downloaded yet")
        if voice is None:
            choices = [v for v in catalog.voices_for(entry.id) if is_installed(v)]
            voice = choices[0] if choices else None
        return self._loaded(self._instance(entry, lambda: make_tts(entry)), "tts"), voice

    def wake_spotter(self) -> VoskWake | None:
        if not self.enabled():
            return None
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
        watcher = self._watcher_for(ctx, raw)
        llm = self.vision_llm(agent) if watcher is not None and watcher.latest_jpeg else None
        try:
            llm = llm or self.llm("local_response", agent)
        except ModelUnavailable as exc:
            if ctx is not None:
                ctx.think(f"Local response model unavailable: {exc.reason}")
            if exc.queueable:
                raise
            return None
        messages = self.build_messages(agent, prompt, system, ctx, with_memory, raw)
        if watcher is not None:
            note = watcher.context_note()
            if note:
                messages[0]["content"] = (messages[0]["content"] + "\n\n" + note) if messages[0]["role"] == "system" \
                    else note
            if llm is self.vision_llm(agent) and watcher.latest_images:   # it can look at the latest frame
                from ..daemon.watcher import image_parts
                messages[-1] = {"role": "user", "content": image_parts(watcher.latest_images) + [
                    {"type": "text", "text": messages[-1]["content"]}]}
        on_token = (lambda t: ctx.think(t, append=True)) if ctx is not None else None
        cancelled = ctx.is_cancelled if ctx is not None else None
        max_tokens = int(self.settings.get("models.local_response.max_tokens", 512))
        try:
            reply = llm.chat(messages, max_tokens=max_tokens, on_token=on_token, cancelled=cancelled).strip()
            if not raw and reply and persona.has_persona(agent) and agent.get("persona_check"):
                reply = self._keep_in_character(llm, agent, reply, ctx, max_tokens)
            return reply
        except BackendError as exc:
            if ctx is not None:
                ctx.think(f"Model error: {exc}")
            return None

    def _watcher_for(self, ctx: Any, raw: bool) -> Any:
        """The agent's screen watcher, when this is one of its other answers (not the watcher itself)."""
        if raw or ctx is None or not hasattr(ctx, "engine"):
            return None
        w = getattr(ctx.engine, "watchers", {}).get(getattr(ctx, "agent_id", None))
        return w if w is not None and w.ctx is not ctx else None

    def build_messages(self, agent: dict[str, Any], prompt: str, system: str = "", ctx: Any = None,
                       with_memory: bool = False, raw: bool = False) -> list[dict[str, str]]:
        """The chat for a reply: character first, then the task, memory and style; the agent's own
        earlier turns as real turns, other agents' as a note; a character reminder at the end."""
        messages: list[dict[str, str]] = []
        sys_parts = []
        in_character = not raw and persona.has_persona(agent)
        if in_character:
            sys_parts.append(persona.identity_block(agent))
        if system:
            sys_parts.append(system)
        if with_memory and ctx is not None:
            from ..config import agent_memory
            am = agent_memory(agent, self.settings)
            mem = ctx.engine.memory.context_for(ctx.agent_id, am["notes"], am["own_only"])
            if mem:
                sys_parts.append(mem)
            recent = ctx.engine.recent_for(ctx.agent_id, agent, am["recent"], am["own_only"],
                                           ctx.request.get("id"))
            others = [r for r in recent if r.get("agent") not in (None, ctx.agent_id)]
            note = persona.others_note(others, {k: v.get("name", k) for k, v in ctx.engine.agents().items()})
            if note:
                sys_parts.append(note)
            for r in recent:
                if r in others:
                    continue
                messages.append({"role": "user", "content": r["text"]})
                if r.get("result"):
                    messages.append({"role": "assistant", "content": str(r["result"])})
        if not raw:
            sys_parts.append("Your reply is read aloud: plain spoken sentences, no markdown, lists or code unless "
                             "asked. Keep it short unless asked for detail -- but short still sounds like you.")
        if sys_parts:
            messages.insert(0, {"role": "system", "content": "\n\n".join(sys_parts)})
        messages.append({"role": "user", "content": f"{prompt}\n\n{persona.reminder(agent)}" if in_character
                         else prompt})
        return messages

    def _keep_in_character(self, llm: Any, agent: dict[str, Any], reply: str, ctx: Any, max_tokens: int) -> str:
        """Agents > Personality > Check replies: grade the reply; rewrite it once if it's off."""
        score, why = persona.judge(llm.chat, agent, reply)
        if ctx is not None:
            ctx.think(f"\nCharacter check: {score}/5 ({why})")
        if score >= 3:
            return reply
        fixed = llm.chat(persona.rewrite_messages(agent, reply), max_tokens=max_tokens).strip()
        if ctx is not None:
            ctx.trace("rewritten_in_character", before=reply, score=score, why=why)
        return fixed or reply

    def vision_llm(self, agent: dict[str, Any] | None = None) -> LLM | None:
        """A model that can look at images: the chosen vision model, or the local response model if it
        can see. None = screen watching works from OCR text instead."""
        for kind in ("vision", "local_response"):
            entry = catalog.get(self.model_id(kind, agent))
            if entry is not None and entry.vision and is_installed(entry):
                try:
                    return self.llm(kind, agent)
                except ModelUnavailable:
                    return None
        return None

    def test_persona(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Agents > Personality > Test personality."""
        try:
            llm = self.llm("local_response", agent)
        except ModelUnavailable as exc:
            return {"error": exc.reason, "results": []}

        def answer(q: str) -> str | None:
            return self.respond(dict(agent, persona_check=False), q)
        return persona.test(answer, llm.chat, agent)

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

    def prune(self) -> list[str]:
        """Unload models that no setting points at any more (switching models used to
        leave every previous one loaded, eating RAM/VRAM until something got killed)."""
        wanted: set[str] = set()
        for kind in KINDS:
            wanted |= self._ids_for_kind(kind)
        dropped = []
        with self._lock:
            for mid in list(self.instances):
                if mid not in wanted:
                    inst = self.instances.pop(mid)
                    try:
                        inst.unload()
                    except Exception:
                        log.exception("unloading %s failed", mid)
                    dropped.append(mid)
        if dropped:
            log.info("unloaded models no longer in use: %s", ", ".join(dropped))
            self.publish("models", self.status())
        return dropped

    def unload_all(self) -> None:
        with self._lock:
            for inst in self.instances.values():
                inst.unload()
            if self.wake is not None:
                self.wake.unload()
        self.publish("models", self.status())

    # ---- status ----------------------------------------------------------
    def hardware(self) -> dict[str, Any]:
        """Detected hardware, cached for a minute (it doesn't change while running)."""
        import time
        cached = getattr(self, "_hw", None)
        if cached is None or time.time() - cached[0] > 60:
            self._hw = cached = (time.time(), hardware.detect())
        return cached[1]

    def recommend(self) -> dict[str, Any]:
        return hardware.recommend(self.budget())

    def status(self) -> dict[str, Any]:
        hw = self.budget()
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
            "catalog": [dict(m.to_dict(), installed=is_installed(m), runtime=runtime_available(m.engine),
                             fit=hardware.fit(m, hw), hardware=catalog.hardware_label(m),
                             speed_label=catalog.SPEED_LABELS.get(m.speed, ""),
                             quality_label=catalog.QUALITY_LABELS.get(m.quality, ""))
                        for m in catalog.CATALOG],
            "hardware": hw,
            "gpu_offload": self.gpu_offload,
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
        if not self.enabled():
            return
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
