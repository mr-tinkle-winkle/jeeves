"""Agents page: who listens for what, with which voice and functions."""
from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QPlainTextEdit, QVBoxLayout, QWidget

from .ui_kit import CustomButton, CustomCheckBox, CustomGroupBox, CustomLineEdit, CustomDoubleSpinBox, show_message
from .widgets import Page, combo, is_locked, label, row

LISTEN = [("Just me (microphone)", "user"), ("Just desktop audio", "desktop"), ("Both", "both")]
OUTPUT = [("Speakers", "speakers"), ("Microphone (Jeeves-Microphone source)", "microphone"), ("Both", "both")]


class AgentsPage(Page):
    def __init__(self, daemon: Any) -> None:
        super().__init__("Agents", "Each agent has its own call names, voice, models, personality and functions. "
                                   "Several agents can be active at once.")
        self.daemon = daemon
        self.agents: dict[str, dict[str, Any]] = {}
        self.functions: list[dict[str, Any]] = []
        self.catalog: list[dict[str, Any]] = []
        self.locked: set[str] = set()
        self.current: str | None = None

        top = QHBoxLayout()
        self.picker = combo()
        self.picker.activated.connect(lambda i: self.select(self.picker.itemData(i)))
        add = CustomButton("+ New agent")
        add.clicked.connect(self.add_agent)
        self.delete_btn = CustomButton("Delete")
        self.delete_btn.clicked.connect(self.delete_agent)
        top.addWidget(label("Agent:", False))
        top.addWidget(self.picker, 1)
        top.addWidget(add)
        top.addWidget(self.delete_btn)
        self.body_layout.addLayout(top)

        self.form = QWidget()
        f = QVBoxLayout(self.form)
        f.setContentsMargins(0, 0, 0, 0)
        f.setSpacing(self._theme.padding)
        self.body_layout.addWidget(self.form)

        # --- identity
        g = self._group(f, "Identity")
        self.enabled = CustomCheckBox("Enabled")
        g.addWidget(self.enabled)
        self.name = CustomLineEdit()
        g.addWidget(row(label("Display name", False), self.name, stretch_last=True))
        self.call_names = CustomLineEdit()
        self.call_names.setPlaceholderText("Jeeves, Butler")
        g.addWidget(row(label("Call names (comma separated)", False), self.call_names, stretch_last=True))
        self.threshold_global = CustomCheckBox("Use the global wake word threshold")
        self.threshold = CustomDoubleSpinBox()
        self.threshold.setRange(0.05, 1.0)
        self.threshold.setSingleStep(0.05)
        self.threshold.setDecimals(2)
        self.threshold_global.toggled.connect(lambda on: self.threshold.setEnabled(not on))
        g.addWidget(row(self.threshold_global, label("or this agent's threshold:", False), self.threshold))
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Default prompt: personality, tone, things to always keep in mind")
        self.prompt.setMinimumHeight(90)
        g.addWidget(label("Default prompt"))
        g.addWidget(self.prompt)

        # --- audio
        g = self._group(f, "Listening and speaking")
        self.listen = combo()
        for lab, v in LISTEN:
            self.listen.addItem(lab, v)
        g.addWidget(row(label("Listens to", False), self.listen))
        self.output = combo()
        for lab, v in OUTPUT:
            self.output.addItem(lab, v)
        g.addWidget(row(label("Speaks through", False), self.output))
        self.show_output = CustomCheckBox("Also show responses on screen")
        g.addWidget(self.show_output)
        self.color_edit = CustomLineEdit()
        self.color_edit.setPlaceholderText("#808080 (empty = stage colors only)")
        g.addWidget(row(label("Spinner color while thinking", False), self.color_edit, stretch_last=True))

        # --- when active
        g = self._group(f, "When this agent is active")
        g.addWidget(label("App names are matched against open windows and running processes (part of the name is "
                          "enough). Leave empty for always."))
        self.enable_open = CustomLineEdit()
        self.enable_focus = CustomLineEdit()
        self.disable_open = CustomLineEdit()
        self.disable_focus = CustomLineEdit()
        for lab, w in (("Only while one of these is open", self.enable_open),
                       ("Only while one of these is focused", self.enable_focus),
                       ("Off while one of these is open", self.disable_open),
                       ("Off while one of these is focused", self.disable_focus)):
            w.setPlaceholderText("obs, steam")
            g.addWidget(row(label(lab, False), w, stretch_last=True))

        # --- models
        g = self._group(f, "Model overrides (empty = global choice)")
        self.model_boxes: dict[str, Any] = {}
        for kind, title in (("stt", "Speech to text"), ("intent", "Intention processing"),
                            ("tts", "Text to speech"), ("tts_voice", "Voice"),
                            ("local_response", "Local response")):
            c = combo()
            self.model_boxes[kind] = c
            g.addWidget(row(label(title, False), c, stretch_last=True))

        # --- functions
        g = self._group(f, "Functions")
        g.addWidget(label("Which functions this agent can run (unticked = never chosen by the intent model)."))
        self.func_box = QWidget()
        self.func_layout = QVBoxLayout(self.func_box)
        self.func_layout.setContentsMargins(0, 0, 0, 0)
        g.addWidget(self.func_box)
        self.func_checks: dict[str, CustomCheckBox] = {}

        g = self._group(f, "Handoff")
        g.addWidget(label("Agents this one may pass information to (Handoff function)."))
        self.any_handoff = CustomCheckBox("Any agent")
        g.addWidget(self.any_handoff)
        self.handoff_box = QWidget()
        self.handoff_layout = QVBoxLayout(self.handoff_box)
        self.handoff_layout.setContentsMargins(24, 0, 0, 0)
        g.addWidget(self.handoff_box)
        self.any_handoff.toggled.connect(lambda on: self.handoff_box.setEnabled(not on))
        self.handoff_checks: dict[str, CustomCheckBox] = {}

        save = CustomButton("Save agent")
        save.clicked.connect(self.save)
        self.body_layout.addWidget(save, alignment=Qt.AlignRight)
        self.finish()

    def _group(self, layout: QVBoxLayout, title: str) -> QVBoxLayout:
        box = CustomGroupBox(title)
        lay = box.make_layout(QVBoxLayout)
        layout.addWidget(box)
        return lay

    # ------------------------------------------------------------------
    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.agents = copy.deepcopy(settings.get("agents") or {})
        self.locked = locked
        self.picker.clear()
        for aid, a in self.agents.items():
            self.picker.addItem(f"{a.get('name', aid)}  ({aid})", aid)
        if self.current not in self.agents:
            self.current = next(iter(self.agents), None)
        self.daemon.call("functions.list", self._got_functions, lambda _e: None)
        self.daemon.call("models.status", self._got_models, lambda _e: None)

    def _got_functions(self, funcs: Any) -> None:
        self.functions = [f for f in (funcs or []) if f["kind"] == "full"]
        for cb in self.func_checks.values():
            cb.setParent(None)
        self.func_checks = {}
        for f in sorted(self.functions, key=lambda f: f["title"]):
            cb = CustomCheckBox(f"{f['title']} — {f['description'][:90]}")
            self.func_checks[f["name"]] = cb
            self.func_layout.addWidget(cb)
        self.select(self.current)

    def _got_models(self, st: Any) -> None:
        self.catalog = (st or {}).get("catalog", [])
        kinds = {"stt": "stt", "intent": "llm", "tts": "tts", "tts_voice": "voice", "local_response": "llm"}
        for kind, cbox in self.model_boxes.items():
            cbox.clear()
            cbox.addItem("(global choice)", None)
            for m in self.catalog:
                if m["kind"] == kinds[kind]:
                    cbox.addItem(f"{m['name']}{'' if m['installed'] else '  (not downloaded)'}", m["id"])
        self.select(self.current)

    def select(self, aid: str | None) -> None:
        self.current = aid
        a = self.agents.get(aid or "")
        self.form.setEnabled(a is not None)
        if a is None:
            return
        i = self.picker.findData(aid)
        self.picker.setCurrentIndex(max(0, i))
        self.enabled.setChecked(a.get("enabled", True))
        self.name.setText(a.get("name", ""))
        self.call_names.setText(", ".join(a.get("call_names", [])))
        self.threshold_global.setChecked(a.get("threshold") is None)
        self.threshold.setValue(a.get("threshold") or 0.6)
        self.prompt.setPlainText(a.get("prompt", ""))
        self.listen.setCurrentIndex(max(0, self.listen.findData(a.get("listen_to", "user"))))
        self.output.setCurrentIndex(max(0, self.output.findData(a.get("output_to", "speakers"))))
        self.show_output.setChecked(a.get("show_output", True))
        self.color_edit.setText(a.get("indicator_color") or "")
        self.enable_open.setText(", ".join(a.get("enable_when_open", [])))
        self.enable_focus.setText(", ".join(a.get("enable_when_focused", [])))
        self.disable_open.setText(", ".join(a.get("disable_when_open", [])))
        self.disable_focus.setText(", ".join(a.get("disable_when_focused", [])))
        for kind, cbox in self.model_boxes.items():
            cbox.setCurrentIndex(max(0, cbox.findData((a.get("models") or {}).get(kind))))
        per = a.get("functions") or {}
        for f in self.functions:
            cb = self.func_checks.get(f["name"])
            if cb:
                cb.setChecked(bool(per.get(f["name"], f["globally_enabled"])))
        for cb in self.handoff_checks.values():
            cb.setParent(None)
        self.handoff_checks = {}
        allowed = a.get("handoff_to") or []
        self.any_handoff.setChecked("*" in allowed)
        for other, oa in self.agents.items():
            if other == aid:
                continue
            cb = CustomCheckBox(oa.get("name", other))
            cb.setChecked(other in allowed)
            self.handoff_checks[other] = cb
            self.handoff_layout.addWidget(cb)
        lk = is_locked(self.locked, f"agents.{aid}")
        self.form.setEnabled(not lk)
        self.delete_btn.setEnabled(not lk)
        if lk:
            self.form.setToolTip("This agent is declared in NixOS")

    def _collect(self) -> dict[str, Any]:
        a = copy.deepcopy(self.agents.get(self.current or "", {}))
        split = lambda w: [x.strip() for x in w.text().split(",") if x.strip()]  # noqa: E731
        a.update({
            "enabled": self.enabled.isChecked(), "name": self.name.text().strip() or self.current,
            "call_names": split(self.call_names) or [self.name.text().strip() or self.current],
            "threshold": None if self.threshold_global.isChecked() else round(self.threshold.value(), 2),
            "prompt": self.prompt.toPlainText(), "listen_to": self.listen.currentData(),
            "output_to": self.output.currentData(), "show_output": self.show_output.isChecked(),
            "indicator_color": self.color_edit.text().strip() or None,
            "enable_when_open": split(self.enable_open), "enable_when_focused": split(self.enable_focus),
            "disable_when_open": split(self.disable_open), "disable_when_focused": split(self.disable_focus),
            "models": {k: c.currentData() for k, c in self.model_boxes.items()},
            "functions": {name: cb.isChecked() for name, cb in self.func_checks.items()},
            "handoff_to": ["*"] if self.any_handoff.isChecked() else
            [k for k, cb in self.handoff_checks.items() if cb.isChecked()],
        })
        return a

    def save(self) -> None:
        if not self.current:
            return
        self.daemon.call("agents.save", lambda _r: show_message(self, "Agents", "Saved."), None,
                         id=self.current, agent=self._collect())

    def add_agent(self) -> None:
        name, ok = QInputDialog.getText(self, "New agent", "Name (it's also the first call name):")
        if not ok or not name.strip():
            return
        aid = "".join(ch for ch in name.lower().strip().replace(" ", "_") if ch.isalnum() or ch in "_-") or "agent"
        while aid in self.agents:
            aid += "_2"

        def made(agent: Any) -> None:
            agent["name"] = name.strip()
            agent["call_names"] = [name.strip()]
            self.current = aid
            self.daemon.call("agents.save", None, None, id=aid, agent=agent)
        self.daemon.call("agents.default", made, None, name=name.strip())

    def delete_agent(self) -> None:
        if self.current:
            self.daemon.call("agents.delete", None, None, id=self.current)
            self.current = None
