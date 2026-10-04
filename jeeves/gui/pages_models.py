"""Models page: choose, download and remove models; unload rules; wake word."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .ui_kit import CustomButton, show_message
from .widgets import Binder, Page, label

KIND_TITLES = {
    "stt": ("Speech to text", "stt", "models.stt.model"),
    "intent": ("Intention processing", "llm", "models.intent.model"),
    "local_response": ("Local AI model for full responses", "llm", "models.local_response.model"),
    "tts": ("Text to speech", "tts", "models.tts.model"),
}


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

        w = self.section("Wake word")
        self.b.check(w, "Listen for agent names", "wake_word.enabled",
                     hint="Off while the Summary log is on (names are found in its transcript instead).")
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
        self.b.number(p, "GPU layers (llama.cpp -ngl)", "models.gpu_layers", 0, 200, 1,
                      hint="0 = CPU only. Higher offloads more of the model to the GPU.")
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
                            "endpoint:http://localhost:11434/v1|model-name as the model."))
        self.catalog_box = QWidget()
        self.catalog_layout = QVBoxLayout(self.catalog_box)
        self.catalog_layout.setContentsMargins(0, 0, 0, 0)
        cat.addWidget(self.catalog_box)
        self.rows: dict[str, tuple[QLabel, CustomButton]] = {}
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self._settings, self._locked = settings, locked
        self.daemon.call("models.status", self._got, lambda _e: None)

    def _fill(self, box: Any, kind: str) -> None:
        cur = box.currentData()
        box.clear()
        box.addItem("(none)", None)
        for m in self.catalog:
            if m["kind"] == kind:
                note = "" if m.get("runtime", True) else "  — engine not installed"
                note = note or ("" if m["installed"] else "  — not downloaded")
                box.addItem(f"{m['name']}{note}", m["id"])
        i = box.findData(cur)
        box.setCurrentIndex(max(0, i))

    def _got(self, st: Any) -> None:
        if not st:
            return
        self.catalog = st["catalog"]
        self._fill(self.wake_model, "wake")
        for kind, (_t, cat_kind, _p) in KIND_TITLES.items():
            self._fill(self.kind_boxes[kind], cat_kind)
        self._fill(self.voice_box, "voice")
        self.b.load(self._settings, self._locked)
        if not self.rows:
            for m in self.catalog:
                if m["builtin"] and not m["files"]:
                    continue
                r = QWidget()
                h = QHBoxLayout(r)
                h.setContentsMargins(0, 0, 0, 0)
                info = label(f"<b>{m['name']}</b> · {m['kind']} · {human(m['size_mb'])} download, ~{human(m['ram_mb'])} "
                             f"RAM<br>{m['description']}")
                state = QLabel("")
                btn = CustomButton("")
                btn.clicked.connect(lambda _=False, mid=m["id"]: self._toggle(mid))
                h.addWidget(info, 1)
                h.addWidget(state)
                h.addWidget(btn)
                self.catalog_layout.addWidget(r)
                self.rows[m["id"]] = (state, btn)
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
        self._st = st

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
