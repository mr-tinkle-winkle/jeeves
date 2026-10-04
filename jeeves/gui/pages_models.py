"""Models page: choose, download and remove models; unload rules; wake word."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .ui_kit import CustomButton, CustomCheckBox, CustomLineEdit, CustomSpinBox, show_message
from .widgets import Binder, Page, clear_layout, combo, get_path as _get, label

KIND_TITLES = {
    "stt": ("Speech to text", "stt", "models.stt.model"),
    "intent": ("Intention processing", "llm", "models.intent.model"),
    "local_response": ("Local AI model for full responses", "llm", "models.local_response.model"),
    "tts": ("Text to speech", "tts", "models.tts.model"),
}


def dots(n: int) -> str:
    return "●" * int(n) + "○" * (5 - int(n))


def human(mb: int) -> str:
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb} MB"


class ModelsPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Models", "Bigger models are more accurate and use more memory. Nothing is downloaded "
                                   "until you pick it. Large models can be unloaded while certain apps are open "
                                   "(games, editors) and come back when they close.")
        self.daemon = daemon
        self.b = Binder(send)
        self.catalog: list[dict[str, Any]] = []
        self.recs: dict[str, Any] = {}
        self._settings: dict[str, Any] = {}
        self._locked: set[str] = set()

        r = self.section("Recommended for your computer")
        self.hw_label = label("Checking your hardware…")
        r.addWidget(self.hw_label)
        self.rec_box = QWidget()
        self.rec_layout = QVBoxLayout(self.rec_box)
        self.rec_layout.setContentsMargins(0, 0, 0, 0)
        r.addWidget(self.rec_box)

        w = self.section("Wake word")
        self.b.check(w, "Always listen for agent names", "wake_word.enabled",
                     hint="Off: the microphone only listens during a Voice Request (keybind or jeeves "
                          "--manual_request=voice) or while an agent waits for an answer. Also off while the "
                          "Summary log is on (names are found in its transcript instead).")
        self.b.choice(w, "Engine", "wake_word.engine",
                      [("Vosk keyword spotter (light, recommended)", "vosk"),
                       ("Transcribe everything with the STT model", "stt-match")])
        self.wake_model = self.b.choice(w, "Wake word model", "wake_word.model", [("(none)", None)])
        self.b.check(w, "Use one global threshold (agents can still override)", "wake_word.global_threshold_enabled")
        self.b.number(w, "Global threshold", "wake_word.global_threshold", 0.05, 1.0, 0.05, 2,
                      hint="How sure the model must be that you said a name. Higher = fewer false starts.")

        self.kind_boxes: dict[str, Any] = {}
        for kind, (title, cat_kind, path) in KIND_TITLES.items():
            s = self.section(title)
            self.kind_boxes[kind] = self.b.choice(s, "Model", path, [("(none)", None)])
            if kind == "tts":
                self.voice_box = self.b.choice(s, "Voice", "models.tts_voice", [("(none)", None)],
                                               hint="Voices belong to a TTS engine; agents can pick their own.")
            self.b.list_text(s, "Unload while these apps are open", f"models.{kind}.unload_when_open",
                             placeholder="steam, blender",
                             hint="Using it meanwhile flashes the indicator red and queues the request.")
            if kind == "stt":
                self.b.text(s, "Language", "models.stt.language", placeholder="en")
            if kind == "local_response":
                self.b.number(s, "Longest answer (tokens)", "models.local_response.max_tokens", 64, 8192, 64)

        p = self.section("Performance")
        self._gpu_layers_row(p)
        self.b.number(p, "CPU threads", "models.threads", 0, 128, 1, hint="0 = automatic")
        self.b.choice(p, "Thinking models (Qwen3, gpt-oss)", "models.reasoning",
                      [("Answer straight away (fast)", "off"), ("Think first when the model wants to (smarter, slower)", "auto")],
                      hint="Thinking shows up in the indicator's thoughts view; it's never spoken. Takes effect "
                           "when the model next loads (Unload all models now).")
        unload = CustomButton("Unload all models now")
        unload.clicked.connect(lambda: daemon.call("models.unload_all", None, None))
        p.addWidget(unload)

        cat = self.section("Download models")
        cat.addWidget(label("Text models (LLMs) can serve intent processing and local responses. An existing "
                            "OpenAI-compatible server (Ollama, LM Studio) can be used by typing "
                            "endpoint:http://localhost:11434/v1|model-name as the model. "
                            "Star models to keep them at the top of every list."))
        bar = QHBoxLayout()
        self.f_kind, self.f_fit, self.f_sort = combo(), combo(), combo()
        for lab, v in (("Every kind", None), ("Text models (LLMs)", "llm"), ("Speech to text", "stt"),
                       ("Wake word", "wake"), ("Voices", "voice"), ("TTS engines", "tts")):
            self.f_kind.addItem(lab, v)
        for lab, v in (("Any hardware", None), ("Runs on my computer", "fits"), ("Quick on my computer", "quick")):
            self.f_fit.addItem(lab, v)
        for lab, v in (("Most capable first", "quality"), ("Fastest first", "speed"), ("Smallest first", "size")):
            self.f_sort.addItem(lab, v)
        self.f_fav = CustomCheckBox("Favorites only")
        self.f_search = CustomLineEdit()
        self.f_search.setPlaceholderText("Search models")
        for w in (self.f_kind, self.f_fit, self.f_sort):
            w.activated.connect(lambda _i: self._render_catalog())
        self.f_fav.toggled.connect(lambda _v: self._render_catalog())
        self.f_search.textChanged.connect(lambda _t: self._render_catalog())
        for w in (self.f_search, self.f_kind, self.f_fit, self.f_sort, self.f_fav):
            bar.addWidget(w, 1 if w is self.f_search else 0)
        cat.addLayout(bar)
        self.catalog_box = QWidget()
        self.catalog_layout = QVBoxLayout(self.catalog_box)
        self.catalog_layout.setContentsMargins(0, 0, 0, 0)
        cat.addWidget(self.catalog_box)
        self.rows: dict[str, tuple[QLabel, CustomButton]] = {}
        self.finish()

    def _gpu_layers_row(self, layout: QVBoxLayout) -> None:
        """Auto / CPU only / All / a number."""
        mode = combo()
        for lab, v in (("Auto (recommended)", "auto"), ("CPU only", 0), ("All layers", 99), ("A number…", "n")):
            mode.addItem(lab, v)
        num = CustomSpinBox()
        num.setRange(1, 200)
        num.wheelEvent = lambda ev: ev.ignore()
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(mode, 1)
        h.addWidget(num)

        def emit() -> None:
            v = mode.currentData()
            num.setVisible(v == "n")
            self.b._emit("models.gpu_layers", num.value() if v == "n" else v)

        def setter(v: Any) -> None:
            if v in ("auto", 0, 99, None):
                mode.setCurrentIndex(max(0, mode.findData("auto" if v is None else v)))
                num.setVisible(False)
            else:
                mode.setCurrentIndex(mode.findData("n"))
                num.setValue(int(v))
                num.setVisible(True)
        mode.activated.connect(lambda _i: emit())
        num.editingFinished.connect(emit)
        self.b.form_row(layout, "GPU layers", box, "models.gpu_layers", setter,
                        hint="A model is a stack of layers that every word passes through. Layers on the GPU run "
                             "many times faster than on the CPU, but each one takes VRAM (its share of the model plus "
                             "its share of the conversation memory). Too many and the model won't load (out of "
                             "memory); too few and the CPU half slows everything down. Auto measures free VRAM "
                             "each time a model loads and puts as many layers on the GPU as fit. Only works if "
                             "llama.cpp has a GPU backend (see Recommended, above).")
        self.offload_label = label("")
        self.offload_label.setContentsMargins(4, 0, 0, 4)
        layout.addWidget(self.offload_label)

    def _show_offload(self, st: dict[str, Any]) -> None:
        lines = []
        for mid, o in (st.get("gpu_offload") or {}).items():
            name = o.get("model", mid)
            if o.get("of"):
                where = "all" if o.get("layers", 0) >= o["of"] else f"{o.get('layers', 0)} of {o['of']}"
                lines.append(f"{name}: {where} layers on the GPU — {o.get('why', '')}")
            else:
                lines.append(f"{name}: {o.get('layers', 0)} layers on the GPU ({o.get('why', '')})")
        self.offload_label.setText("Last load: " + "; ".join(lines) if lines else "")

    # ------------------------------------------------------------------ data
    def favorites(self) -> list[str]:
        return list((self._settings.get("models") or {}).get("favorites") or [])

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self._settings, self._locked = settings, locked
        self.daemon.call("models.status", self._got, lambda _e: None)
        self.daemon.call("models.recommend", self._got_recs, lambda _e: None)

    def _fill(self, box: Any, kind: str) -> None:
        cur = box.currentData()
        box.clear()
        box.addItem("(none)", None)
        favs = self.favorites()
        rec_ids = {r["id"] for r in self.recs.values()}
        items = [m for m in self.catalog if m["kind"] == kind]
        items.sort(key=lambda m: (m["id"] not in favs, -m.get("quality", 3)))
        for m in items:
            note = "" if m.get("runtime", True) else "  — engine not installed"
            note = note or ("" if m["installed"] else "  — not downloaded")
            star = "★ " if m["id"] in favs else ""
            rec = "  (recommended)" if m["id"] in rec_ids else ""
            box.addItem(f"{star}{m['name']}{rec}{note}", m["id"])
        i = box.findData(cur)
        box.setCurrentIndex(max(0, i))

    def _got(self, st: Any) -> None:
        if not st:
            return
        self._st = st
        self.catalog = st["catalog"]
        self._show_offload(st)
        self._fill(self.wake_model, "wake")
        for kind, (_t, cat_kind, _p) in KIND_TITLES.items():
            self._fill(self.kind_boxes[kind], cat_kind)
        self._fill(self.voice_box, "voice")
        self.b.load(self._settings, self._locked)
        self._render_catalog()

    # ------------------------------------------------------------------ your computer
    def _got_recs(self, res: Any) -> None:
        if not res:
            return
        sig = repr((res, (self._settings.get("models") or {}), (self._settings.get("wake_word") or {}).get("model")))
        if sig == getattr(self, "_recs_sig", None):
            return
        self._recs_sig = sig
        hw, picks = res["hardware"], res["picks"]
        self.recs = picks
        gpus = ", ".join(f"{g['name']} ({g['vram_mb'] / 1000:.0f} GB)" for g in hw["gpus"]) or "none detected"
        lines = [f"<b>Your computer:</b> {hw['ram_mb'] / 1000:.0f} GB RAM · {hw['cpu_threads']} CPU threads · GPU: {gpus}"]
        if hw["gpus"] and not hw["llama_gpu"]:
            lines.append("Your GPU isn't being used: the installed llama.cpp is CPU-only. On NixOS set "
                         "<code>services.jeeves.acceleration = \"vulkan\";</code> (or \"cuda\" for NVIDIA, \"rocm\" "
                         "for AMD) and rebuild for much faster answers and bigger models.")
        elif hw["llama_gpu"]:
            lines.append(f"llama.cpp can use the GPU ({', '.join(hw['llama_gpu'])}). GPU layers (below) on Auto "
                         "fits as much of each model on it as your free VRAM allows.")
        self.hw_label.setText("<br>".join(lines))
        clear_layout(self.rec_layout)
        roles = [("wake", "Wake word", "wake_word.model"), ("stt", "Speech to text", "models.stt.model"),
                 ("intent", "Intention processing", "models.intent.model"),
                 ("local_response", "Local responses", "models.local_response.model"),
                 ("local_response_fast", "  …or faster", "models.local_response.model"),
                 ("local_response_smart", "  …or smarter", "models.local_response.model"),
                 ("tts", "Text to speech", "models.tts.model"), ("tts_voice", "Voice", "models.tts_voice")]
        for role, title, path in roles:
            pick = picks.get(role)
            if not pick:
                continue
            current = _get(self._settings, path) == pick["id"]
            use = CustomButton("In use" if current else "Use this")
            use.setEnabled(not current)
            use.clicked.connect(lambda _=False, path=path, mid=pick["id"]: self._use(path, mid))
            line = QWidget()
            h = QHBoxLayout(line)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(label(f"<b>{title}:</b> {pick['name']} — {pick['why']}"), 1)
            h.addWidget(use, 0)
            self.rec_layout.addWidget(line)
        if self.catalog:                 # mark the recommended entries in the choosers
            self._fill(self.wake_model, "wake")
            for kind, (_t, cat_kind, _p) in KIND_TITLES.items():
                self._fill(self.kind_boxes[kind], cat_kind)
            self._fill(self.voice_box, "voice")
            self.b.load(self._settings, self._locked)

    def _use(self, path: str, mid: str) -> None:
        m = next((m for m in self.catalog if m["id"] == mid), None)
        self.b.send({path: mid})
        if m is not None and not m["installed"]:
            self.daemon.call("models.download", None, None, id=mid)

    # ------------------------------------------------------------------ catalog
    def _toggle_favorite(self, mid: str) -> None:
        favs = self.favorites()
        favs = [f for f in favs if f != mid] if mid in favs else favs + [mid]
        self.b.send({"models.favorites": favs})

    def _render_catalog(self) -> None:
        favs = self.favorites()
        kind, fitf, sort = self.f_kind.currentData(), self.f_fit.currentData(), self.f_sort.currentData()
        q = self.f_search.text().lower().strip()
        # nothing that's drawn changed: just refresh download states (UI guide pitfall 14)
        sig = (tuple(favs), kind, fitf, sort, q, self.f_fav.isChecked(),
               tuple((m["id"], m["installed"], m.get("fit"), m.get("runtime")) for m in self.catalog))
        if sig == getattr(self, "_catalog_sig", None):
            self._update_rows()
            return
        self._catalog_sig = sig
        clear_layout(self.catalog_layout)
        self.rows = {}
        items = []
        for m in self.catalog:
            if m["builtin"] and not m["files"] and m["kind"] != "voice":
                continue
            if kind and m["kind"] != kind:
                continue
            if fitf == "fits" and m.get("fit") == "no":
                continue
            if fitf == "quick" and m.get("fit") not in ("gpu", "cpu"):
                continue
            if self.f_fav.isChecked() and m["id"] not in favs:
                continue
            if q and q not in (m["name"] + m["description"] + m["id"]).lower():
                continue
            items.append(m)
        order = {"llm": 0, "stt": 1, "wake": 2, "tts": 3, "voice": 4}
        keyf = {"quality": lambda m: (-m.get("quality", 3), -m.get("speed", 3)),
                "speed": lambda m: (-m.get("speed", 3), -m.get("quality", 3)),
                "size": lambda m: (m["size_mb"],)}[sort]
        items.sort(key=lambda m: (m["id"] not in favs, order.get(m["kind"], 9), *keyf(m)))
        heading = None
        titles = {"llm": "Text models (LLMs)", "stt": "Speech to text", "wake": "Wake word", "tts": "TTS engines",
                  "voice": "Voices"}
        for m in items:
            group = "★ Favorites" if m["id"] in favs else titles.get(m["kind"], m["kind"])
            if group != heading:
                heading = group
                h = QLabel(f"<b>{group}</b>")
                h.setContentsMargins(0, 10, 0, 2)
                self.catalog_layout.addWidget(h)
            self.catalog_layout.addWidget(self._row(m, m["id"] in favs))
        if not items:
            self.catalog_layout.addWidget(label("No models match."))
        self._update_rows()

    def _row(self, m: dict[str, Any], fav: bool) -> QWidget:
        r = QWidget()
        h = QHBoxLayout(r)
        h.setContentsMargins(0, 0, 0, 0)
        star = CustomButton("★" if fav else "☆")
        star.setToolTip("Favorite")
        star.setFixedWidth(40)
        star.clicked.connect(lambda _=False, mid=m["id"]: self._toggle_favorite(mid))
        what = {"llm": "Smarts", "stt": "Accuracy", "wake": "Accuracy"}.get(m["kind"], "Naturalness")
        fit = {"gpu": "✓ fast on your GPU", "cpu": "✓ runs well on your computer",
               "slow": "⚠ fits, but slow on your computer", "no": "✗ not enough memory here"}.get(m.get("fit"), "")
        dl = "" if not m["files"] else f" · {human(m['size_mb'])} download"
        params = f" · {m['params_b']:g}B parameters" + (f" ({m['active_b']:g}B active)" if m["active_b"] and
                                                           m["active_b"] < m["params_b"] else "") if m["params_b"] else ""
        info = label(f"<b>{m['name']}</b>{params}<br>"
                     f"Speed: {dots(m['speed'])} {m['speed_label']} · {what}: {dots(m['quality'])} "
                     f"{m['quality_label']} · Needs: {m['hardware']}{dl}<br>{fit} — {m['description']}")
        state = QLabel("")
        btn = CustomButton("")
        btn.clicked.connect(lambda _=False, mid=m["id"]: self._toggle(mid))
        h.addWidget(star, 0)
        h.addWidget(info, 1)
        h.addWidget(state)
        if m["files"] or (m["kind"] == "voice" and not m["builtin"]):
            h.addWidget(btn)
            self.rows[m["id"]] = (state, btn)
        return r

    def _update_rows(self) -> None:
        st = getattr(self, "_st", {})
        for m in self.catalog:
            if m["id"] not in self.rows:
                continue
            state, btn = self.rows[m["id"]]
            dl = st.get("downloads", {}).get(m["id"])
            if dl and dl.get("state") == "downloading":
                pct = 100 * dl["done"] / dl["total"] if dl.get("total") else 0
                state.setText(f"{pct:.0f}%")
                btn.setText("Cancel")
            else:
                state.setText("✓ installed" if m["installed"] else (f"failed: {dl['error']}" if dl and dl.get("state") == "error" else ""))
                if not m.get("runtime", True):
                    state.setText(state.text() + "  (its engine isn't installed)")
                btn.setText("Remove" if m["installed"] else "Download")

    def _toggle(self, mid: str) -> None:
        m = next((m for m in self.catalog if m["id"] == mid), None)
        if m is None:
            return
        dl = getattr(self, "_st", {}).get("downloads", {}).get(mid)
        if dl and dl.get("state") == "downloading":
            self.daemon.call("models.cancel", None, None, id=mid)
        elif m["installed"]:
            self.daemon.call("models.remove", lambda _r: self.refresh(self._settings, self._locked), None, id=mid)
        else:
            self.daemon.call("models.download", None, None, id=mid)

    def on_event(self, topic: str, data: Any) -> None:
        if topic == "download" and data:
            row = self.rows.get(data.get("model"))
            if row is None:
                return
            state, btn = row
            if data.get("state") == "downloading":
                pct = 100 * data["done"] / data["total"] if data.get("total") else 0
                state.setText(f"{pct:.0f}%  (file {data.get('file')}/{data.get('files')})")
                btn.setText("Cancel")
            elif data.get("state") == "error":
                show_message(self, "Download failed", data.get("error", ""))
        elif topic == "models":
            self.refresh(self._settings, self._locked)
