"""Agents page: who listens for what, with which voice and functions."""
from __future__ import annotations

import copy
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .ui_kit import (CustomPlainTextEdit, ask_text, CustomButton, CustomCheckBox, CustomDoubleSpinBox, CustomGroupBox, CustomLineEdit,
                     CustomSpinBox, show_message)
from ..functions.custom_sources import parse as parse_sources
from .command_editor import CommandList
from .widgets import discard, Page, combo, is_locked, label, row
from ..models.jeenius import LEVELS

LISTEN = [("Just me (microphone)", "user"), ("Just desktop audio", "desktop"), ("Both", "both"),
          ("A specific device…", "device")]
OUTPUT = [("Speakers", "speakers"), ("Microphone (Jeeves-Microphone source)", "microphone"), ("Both", "both"),
          ("A specific device…", "device"), ("A specific device + Jeeves-Microphone", "device_mic")]


# Which agent tab each section of the form is on (the window shows one tab of an agent at a time).
TAB_OF = {"Identity": "personality", "Conversation": "personality", "Memory": "personality",
          "When this agent is active": "personality", "Listening and speaking": "voice", "Voice": "voice",
          "Model overrides (empty = global choice)": "brain", "Rudimentary agent": "abilities", "Functions": "abilities", "Commands": "abilities",
          "Handoff": "abilities", "Custom sources": "sources"}


class AgentsPage(Page):
    def __init__(self, daemon: Any, embedded: bool = False) -> None:
        super().__init__("Agents", "Each agent has its own call names, voice, models, personality and functions. "
                                   "Several agents can be active at once.", show_heading=not embedded)
        self.embedded = embedded
        self.tab_groups: dict[str, list[QWidget]] = {}
        self.on_loaded: Any = None          # called when functions/models arrive (the overview's numbers)
        self.daemon = daemon
        self.agents: dict[str, dict[str, Any]] = {}
        self.lobotomize: Any = None              # set by the window: (agent id) -> asks, then wipes
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
        if embedded:                         # (the window's sidebar picks and adds agents)
            for w in (self.picker, add):
                w.hide()
            top.itemAt(0).widget().hide()
            top.insertStretch(0, 1)
        self.body_layout.addLayout(top)

        self.form = QWidget()
        f = QVBoxLayout(self.form)
        f.setContentsMargins(0, 0, 0, 0)
        f.setSpacing(self._theme.padding)
        self.body_layout.addWidget(self.form)

        # --- identity
        g = self._group(f, "Identity")          # (on/off is on the agent itself: its header and right-click)
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
        self.threshold.setToolTip("How sure the wake word model must be that it heard the name: higher = stricter "
                                  "(fewer false wakes, a mumbled name may be missed). 0.6 is the default.")
        g.addWidget(row(self.threshold_global, label("or this agent's threshold:", False), self.threshold))
        self.wake_reply = CustomLineEdit()
        self.wake_reply.setPlaceholderText("Yes? | Hello. | At your service.")
        self.wake_reply.setToolTip("Said when it starts listening and you're waiting for it (not when you say the "
                                   "name and the request in one go). Several, separated by |, are picked at "
                                   "random. Empty = says nothing.")
        g.addWidget(row(label("Wake-up reply", False), self.wake_reply, stretch_last=True))
        self.jeenius = combo()
        self.jeenius.addItem("(global setting, Settings > Models)", 0)
        for n, name in LEVELS.items():
            self.jeenius.addItem(f"{n} - {name}", n)
        self.jeenius.setToolTip("How much it thinks before answering. 1 never thinks (fastest); 2 thinks for "
                                "research and hard questions; 3 thinks unless the request is simple; 4 always "
                                "thinks.")
        g.addWidget(row(label("Jeenius", False), self.jeenius, stretch_last=True))
        from ..personas import PERSONAS, STRENGTHS
        self.persona = combo()
        self.persona.addItem("Your own (write it below)", "")
        for pk, pv in PERSONAS.items():
            self.persona.addItem(f"{pv.name} — {pv.blurb}", pk)
        self.strength = combo()
        for st in STRENGTHS:
            self.strength.addItem(st.capitalize(), st)
        self.strength.setToolTip("Light: a touch of the character, mostly plain answers (a butler who doesn't keep "
                                 "offering tea). Medium: clearly the character, the answer first. Strong: fully in "
                                 "character.")
        self.persona.activated.connect(lambda _i: self._apply_persona())
        self.strength.activated.connect(lambda _i: self._apply_persona())
        g.addWidget(row(label("Start from", False), self.persona, label("Strength", False), self.strength, stretch=1))
        self.prompt = CustomPlainTextEdit()
        self.prompt.setPlaceholderText("Default prompt: personality, tone, things to always keep in mind")
        self.prompt.setMinimumHeight(90)
        g.addWidget(label("Personality (default prompt)"))
        g.addWidget(self.prompt)
        g.addWidget(label("Who the agent is and how it talks. Be specific: \u201cDry, formal British butler. Calls "
                          "the user sir. Short sentences, understated wit, never says 'awesome'.\u201d beats \u201cbe a "
                          "butler\u201d. It comes first in everything the agent is asked, so keep it to the point."))
        self.persona_check = CustomCheckBox("Check replies stay in character (rewrites ones that don't; one or two "
                                            "extra model calls per reply)")
        g.addWidget(self.persona_check)
        self.every_line = CustomCheckBox("Every line in character: functions' plain lines (\"Timer set for 2:00.\") are "
                                         "said the way it would say them (a short model call each)")
        g.addWidget(self.every_line)
        self.persona_btn = CustomButton("Test personality")
        self.persona_btn.clicked.connect(self.test_persona)
        g.addWidget(row(self.persona_btn))
        self.persona_result = CustomPlainTextEdit()
        self.persona_result.setReadOnly(True)
        self.persona_result.setMaximumHeight(170)
        self.persona_result.setPlaceholderText("Asks the agent a few questions with the prompt above (saved or not) "
                                               "and grades each answer for staying in character.")
        g.addWidget(self.persona_result)

        # --- audio
        g = self._group(f, "Listening and speaking")
        self.listen = combo()
        for lab, v in LISTEN:
            self.listen.addItem(lab, v)
        g.addWidget(row(label("Listens to", False), self.listen))
        self.device = combo()
        self.device_row = row(label("Device", False), self.device, stretch_last=True)
        g.addWidget(self.device_row)
        self.device_hint = label("Microphones, or an output's monitor (everything that output plays, e.g. the "
                                 "headphones your voice chat comes through).")
        g.addWidget(self.device_hint)
        self.listen.currentIndexChanged.connect(lambda _i: self._device_visibility())
        self.output = combo()
        for lab, v in OUTPUT:
            self.output.addItem(lab, v)
        g.addWidget(row(label("Speaks through", False), self.output))
        self.out_device = combo()
        self.out_device_row = row(label("Output device", False), self.out_device, stretch_last=True)
        g.addWidget(self.out_device_row)
        self.output.currentIndexChanged.connect(lambda _i: self._device_visibility())
        self.show_output = CustomCheckBox("Also show responses on screen")
        g.addWidget(self.show_output)
        self.mute_audio = CustomCheckBox("Mute its voice: it never speaks (no wake-up reply either)")
        self.mute_visual = CustomCheckBox("Mute it on screen: no indicator or text (except when it asks you something)")
        g.addWidget(self.mute_audio)
        g.addWidget(self.mute_visual)
        g.addWidget(label("To let friends in a call hear an agent: set Speaks through to Microphone or Both, "
                          "then pick “Jeeves-Microphone” as your microphone in Discord (it carries your "
                          "voice and the agent's)."))
        self.color_edit = CustomLineEdit()
        self.color_edit.setPlaceholderText("#808080 (empty = stage colors only)")
        g.addWidget(row(label("Spinner color while thinking", False), self.color_edit, stretch_last=True))

        # --- jump in
        g = self._group(f, "Conversation")
        self.jump_in = CustomCheckBox("Jump in whenever the AI wants to")
        g.addWidget(self.jump_in)
        self.jump_freq = CustomDoubleSpinBox()
        self.jump_freq.setRange(0.0, 1.0)
        self.jump_freq.setSingleStep(0.05)
        self.jump_freq.setDecimals(2)
        self.jump_freq.wheelEvent = lambda e: e.ignore()
        g.addWidget(row(label("Frequency", False), self.jump_freq))
        g.addWidget(label("The agent listens to the conversation on its sources and chimes in when it has "
                          "something to say. 1 = a full part of the conversation, 0.5 = when it has something "
                          "useful or funny, 0.1 = only for something important, 0 = never. Uses the local "
                          "response model."))
        self.jump_in.toggled.connect(lambda on: self.jump_freq.setEnabled(on))

        # --- memory
        g = self._group(f, "Memory")
        self.mem_enabled = CustomCheckBox("Remember things (past requests and notes)")
        g.addWidget(self.mem_enabled)
        self.mem_recent = CustomSpinBox()
        self.mem_recent.setRange(-1, 50)
        self.mem_recent.setSpecialValueText("Default")
        self.mem_recent.wheelEvent = lambda e: e.ignore()
        g.addWidget(row(label("Recent requests it sees", False), self.mem_recent))
        self.mem_notes = CustomSpinBox()
        self.mem_notes.setRange(0, 500)
        self.mem_notes.wheelEvent = lambda e: e.ignore()
        g.addWidget(row(label("Remembered notes it sees", False), self.mem_notes))
        self.mem_own = CustomCheckBox("Only its own requests and notes")
        g.addWidget(self.mem_own)
        g.addWidget(label("Recent requests let it follow up (\"make it louder\", \"the macro I just made\"); notes "
                          "are what you asked Jeeves to remember. More memory means longer prompts, so slower "
                          "replies on small models. Off: the agent starts fresh every time and can't remember "
                          "anything. Default uses the global count (Listening & Keys)."))
        self.mem_enabled.toggled.connect(self._memory_enabled)
        lob = CustomButton("Lobotomize…")
        lob.setToolTip("Forget everything this agent remembers and start it afresh (its settings stay)")
        lob.clicked.connect(lambda: self.lobotomize(self.current) if self.current and self.lobotomize else None)
        g.addWidget(row(label("Start it afresh: its notes, requests and conversation are forgotten.", False), lob))

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

        # --- voice
        g = self._group(f, "Voice")
        self.model_boxes: dict[str, Any] = {}
        self.voice_filter = CustomLineEdit()
        self.voice_filter.setPlaceholderText("Search voices: scottish, female, deep, kokoro, accent…")
        self.voice_filter.textChanged.connect(lambda _t: self._fill_voices())
        g.addWidget(row(label("Find", False), self.voice_filter, stretch_last=True))
        self.voice_box = combo()
        self.model_boxes["tts_voice"] = self.voice_box
        self.voice_box.currentIndexChanged.connect(lambda _i: self._voice_changed())
        g.addWidget(row(label("Voice", False), self.voice_box, stretch_last=True))
        self.voice_info = label("")
        g.addWidget(self.voice_info)
        self.speaker_box = combo()
        self.speaker_row = row(label("Speaker", False), self.speaker_box, stretch_last=True)
        g.addWidget(self.speaker_row)

        def dspin(lo: float, hi: float, step: float, dec: int = 2, suffix: str = "") -> CustomDoubleSpinBox:
            sp = CustomDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(dec)
            if suffix:
                sp.setSuffix(suffix)
            sp.wheelEvent = lambda e: e.ignore()
            return sp
        self.v_speed = dspin(0.5, 2.0, 0.05, suffix="×")
        self.v_pitch = dspin(-24, 24, 0.5, 1, " semitones")
        self.v_expr = dspin(0.0, 1.2, 0.05)
        g.addWidget(row(label("Speed", False), self.v_speed))
        g.addWidget(row(label("Pitch", False), self.v_pitch))
        self.expr_row = row(label("Expressiveness (Piper)", False), self.v_expr)
        g.addWidget(self.expr_row)
        self.v_effect = combo()
        for key, (title, _chain) in __import__("jeeves.models.voicefx", fromlist=["EFFECTS"]).EFFECTS.items():
            self.v_effect.addItem(title, key)
        g.addWidget(row(label("Effect", False), self.v_effect))
        self.v_blend = combo()
        self.v_blend_amount = dspin(0.0, 1.0, 0.05)
        self.blend_row = row(label("Mix in (Kokoro)", False), self.v_blend, label("amount", False),
                             self.v_blend_amount)
        g.addWidget(self.blend_row)
        self.preview_btn = CustomButton("Preview voice")
        self.preview_btn.clicked.connect(self.preview)
        self.download_voice_btn = CustomButton("Download this voice")
        self.download_voice_btn.clicked.connect(self._download_voice)
        g.addWidget(row(self.preview_btn, self.download_voice_btn))
        self.voice_status = label("Pitch and effects need SoX (included in the Nix package). Mixing two Kokoro "
                                  "voices makes a new one; multi-speaker Piper voices have dozens to hundreds of "
                                  "speakers to pick from.")
        g.addWidget(self.voice_status)

        # --- models
        g = self._group(f, "Model overrides (empty = global choice)")
        for kind, title in (("stt", "Speech to text"), ("intent", "Intention processing"),
                            ("tts", "Text to speech"), ("local_response", "Local response")):
            c = combo()
            self.model_boxes[kind] = c
            g.addWidget(row(label(title, False), c, stretch_last=True))

        # --- a rudimentary agent: just one function, so it only works out how you want it done
        g = self._group(f, "Rudimentary agent")
        self.rud_on = CustomCheckBox("Rudimentary: this agent has just one function")
        g.addWidget(self.rud_on)
        g.addWidget(label("It never has to work out which function you mean, only how you want this one done, so "
                          "it's quicker: a function with nothing to fill in runs as soon as you say the agent's "
                          "name; one with details (a timer's length) has them filled in from a short prompt about "
                          "that function alone, and it asks if one is missing. It never chats or does anything "
                          "else."))
        self.rud_function = combo()
        g.addWidget(row(label("Its function", False), self.rud_function, stretch_last=True))
        # Run commands: what's said is the command line
        self.rud_cmd_box = QWidget()
        cb = QVBoxLayout(self.rud_cmd_box)
        cb.setContentsMargins(0, 0, 0, 0)
        cb.addWidget(label("Run commands: “Box, firefox” runs firefox; “Box, systemctl dash dash user restart "
                           "pipewire” runs systemctl --user restart pipewire (spoken dash → -, dot → ., slash → /, "
                           "tilde → ~; typed requests run exactly as typed; no shell)."))
        self.rud_program = CustomLineEdit()
        self.rud_program.setPlaceholderText("e.g. playerctl  (empty: the first word said is the program)")
        cb.addWidget(row(label("Always run this program, with what I say as its arguments", False),
                         self.rud_program, stretch_last=True))
        self.rud_confirm = combo()
        for text, key in (("Only for risky ones (rm, dd, kill, reboot, sudo…)", "risky"), ("Always", "always"),
                          ("Never", "never")):
            self.rud_confirm.addItem(text, key)
        self.rud_terminal = CustomCheckBox("Run in a terminal window (to watch it, or for sudo)")
        cb.addWidget(row(label("Ask before running", False), self.rud_confirm, self.rud_terminal))
        g.addWidget(self.rud_cmd_box)
        self.rud_on.toggled.connect(self._rudimentary)
        self.rud_function.currentIndexChanged.connect(lambda _i: self._rudimentary(self.rud_on.isChecked()))

        # --- functions
        g = self._group(f, "Functions")
        g.addWidget(label("Which functions this agent can run (unticked = never chosen by the intent model)."))
        self.func_box = QWidget()
        self.func_layout = QVBoxLayout(self.func_box)
        self.func_layout.setContentsMargins(0, 0, 0, 0)
        g.addWidget(self.func_box)
        self.func_checks: dict[str, CustomCheckBox] = {}

        g = self._group(f, "Commands")
        g.addWidget(label("Programs this agent can run for you by name, e.g. “rebuild the laptop” runs your "
                          "rebuild command with host = laptop. Each one shows up in its function list."))
        self.commands = CommandList()
        g.addWidget(self.commands)

        g = self._group(f, "Custom sources")
        self.sources_on = CustomCheckBox("Use these sites when researching")
        g.addWidget(self.sources_on)
        g.addWidget(label("One site per line: its address, then = and what it's for. When a question fits one, "
                          "research reads that site first (and searches inside it) before the rest of the web."))
        self.sources_edit = CustomPlainTextEdit()
        self.sources_edit.setPlaceholderText("https://parkour-reborn.fandom.com/wiki/Movement = Parkour Reborn "
                                             "Movement Wiki, for information on any movement techniques")
        self.sources_edit.setMinimumHeight(90)
        self.sources_edit.setMaximumHeight(150)
        g.addWidget(self.sources_edit)
        self.sources_status = label("")
        g.addWidget(self.sources_status)
        self.sources_edit.textChanged.connect(self._sources_changed)
        self.sources_on.toggled.connect(self.sources_edit.setEnabled)

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
        save.setToolTip("Save agent (Ctrl+S)")
        self.saved_note = QLabel("")
        self.body_layout.addWidget(row(self.saved_note, save, stretch=0), alignment=Qt.AlignRight)
        # saved as you go: a moment after an edit (Settings > General > Save changes automatically)
        from .autosave import AutoSave
        import time as _time
        self._loaded_parts: set[str] = set()
        self._autosaved_at = 0.0
        self.autosave = AutoSave(self, state=lambda: self._collect() if self.current in self.agents else None,
                                 save=self._save_quiet,
                                 ready=lambda: {"functions", "models", "devices"} <= self._loaded_parts
                                 and self.current in self.agents)
        self._now = _time.time
        # Ctrl+S anywhere in the window while this page is open (even with the cursor in a text box)
        self.save_shortcut = QShortcut(QKeySequence(QKeySequence.Save), self)
        self.save_shortcut.setContext(Qt.WindowShortcut)
        self.save_shortcut.activated.connect(lambda: self.isVisible() and self.save())
        self.finish()

    def _apply_persona(self) -> None:
        """A built-in personality at a strength: its text goes into the box (Ctrl+Z brings yours back)."""
        from ..personas import PERSONAS
        p = PERSONAS.get(self.persona.currentData() or "")
        if p is None:
            return
        cur = self.prompt.textCursor()
        cur.select(cur.SelectionType.Document)
        cur.insertText(p.prompt(self.strength.currentData()))

    def _group(self, layout: QVBoxLayout, title: str) -> QVBoxLayout:
        box = CustomGroupBox(title)
        lay = box.make_layout(QVBoxLayout)
        layout.addWidget(box)
        self.tab_groups.setdefault(TAB_OF.get(title, "personality"), []).append(box)
        return lay

    def show_tab(self, tab: str | None) -> None:
        """Only this tab's sections (None: all of them)."""
        for key, boxes in self.tab_groups.items():
            for b in boxes:
                b.setVisible(tab is None or key == tab)
        self.relayout()

    # ------------------------------------------------------------------
    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        incoming = copy.deepcopy(settings.get("agents") or {})
        if self.current in incoming and self.current in self.agents and self._now() - self._autosaved_at < 6 \
                and set(incoming) == set(self.agents):
            # our own autosave coming back: don't fill the form in again under the cursor
            self.agents = incoming
            self.locked = locked
            return
        self.agents = incoming
        self.global_voice = ((settings.get("models") or {}).get("tts_voice")) or ""
        self.locked = locked
        self.picker.clear()
        for aid, a in self.agents.items():
            self.picker.addItem(f"{a.get('name', aid)}  ({aid})", aid)
        if self.current not in self.agents:
            self.current = next(iter(self.agents), None)
        self.daemon.call("functions.list", self._got_functions, lambda _e: None)
        self.daemon.call("models.status", self._got_models, lambda _e: None)
        self.daemon.call("audio.devices", self._got_devices, lambda _e: None)

    def _got_functions(self, funcs: Any) -> None:
        self.functions = [f for f in (funcs or []) if f["kind"] == "full"]
        for cb in self.func_checks.values():
            discard(cb)
        self.func_checks = {}
        for f in sorted(self.functions, key=lambda f: f["title"]):
            cb = CustomCheckBox(f"{f['title']} — {f['description'][:90]}")
            self.func_checks[f["name"]] = cb
            self.func_layout.addWidget(cb)
        self._fill_rud_functions()
        self._loaded_parts.add("functions")
        self.select(self.current)
        if self.on_loaded:
            self.on_loaded()

    def _got_models(self, st: Any) -> None:
        self._loaded_parts.add("models")
        QTimer.singleShot(0, self._settled)
        self.catalog = (st or {}).get("catalog", [])
        kinds = {"stt": "stt", "intent": "llm", "tts": "tts", "tts_voice": "voice", "local_response": "llm"}
        for kind, cbox in self.model_boxes.items():
            if kind == "tts_voice":
                continue
            cbox.clear()
            cbox.addItem("(global choice)", None)
            for m in self.catalog:
                if m["kind"] == kinds[kind]:
                    cbox.addItem(f"{m['name']}{'' if m['installed'] else '  (not downloaded)'}", m["id"])
        self.v_blend.clear()
        self.v_blend.addItem("(none)", "")
        for m in self.catalog:
            if m["kind"] == "voice" and m["engine"] == "kokoro":
                self.v_blend.addItem(m["name"], m["id"])
        self._fill_voices()
        self.select(self.current)
        if self.on_loaded:
            self.on_loaded()

    # ---- voice ----------------------------------------------------------------
    ENGINE_NAMES = {"piper": "Piper", "kokoro": "Kokoro", "espeak-ng": "eSpeak"}

    def _fill_voices(self, want: Any = "keep") -> None:
        box = self.voice_box
        cur = box.currentData() if want == "keep" else want
        words = [w for w in self.voice_filter.text().lower().split() if w]
        box.blockSignals(True)
        box.clear()
        box.addItem("(global choice)", None)
        for m in self.catalog:
            if m["kind"] != "voice":
                continue
            text = f"{self.ENGINE_NAMES.get(m['engine'], m['engine'])} · {m['name']}"
            hay = (text + " " + m.get("description", "")).lower()
            if words and not all(w in hay for w in words) and m["id"] != cur:
                continue
            box.addItem(text + ("" if m["installed"] else "  (not downloaded)"), m["id"])
        box.setCurrentIndex(max(0, box.findData(cur)))
        box.blockSignals(False)
        self._voice_changed(load_speakers=True)

    def _voice_entry(self) -> dict[str, Any] | None:
        """The voice this agent speaks in -- its own choice, or the global one."""
        vid = self.voice_box.currentData() or getattr(self, "global_voice", "")
        return next((m for m in self.catalog if m["id"] == vid), None)

    def _voice_changed(self, load_speakers: bool = True) -> None:
        m = self._voice_entry()
        engine = m["engine"] if m else ""
        multi = bool(m and engine == "piper" and m.get("speakers", 1) > 1)
        self.speaker_row.setVisible(multi)
        self.expr_row.setVisible(engine in ("piper", ""))
        # mixing voices is something Kokoro does (its voices are blendable); shown only for a Kokoro voice --
        # set on a Piper voice it did nothing at all
        self.blend_row.setVisible(engine == "kokoro")
        self.download_voice_btn.setVisible(bool(m and not m["installed"]))
        if m:
            extra = f" {m['speakers']} speakers." if multi and str(m["speakers"]) not in m.get("description", "") else ""
            self.voice_info.setText(f"{m.get('description', '')}{extra}" +
                                    ("" if m["installed"] else " Not downloaded yet."))
        else:
            self.voice_info.setText("Uses the voice chosen on the Models page.")
        if multi and load_speakers:
            self.daemon.call("voice.speakers", self._got_speakers, lambda _e: None, voice=m["id"])

    def _got_speakers(self, res: Any) -> None:
        want = getattr(self, "_want_speaker", "")
        self.speaker_box.clear()
        self.speaker_box.addItem("(default)", "")
        for sp in (res or {}).get("speakers", []):
            self.speaker_box.addItem(sp["name"] + (f" — {sp['label']}" if sp.get("label") else ""), sp["name"])
        if not (res or {}).get("installed"):
            self.speaker_box.addItem("(download the voice to list every speaker)", "")
        if want and self.speaker_box.findData(want) < 0:
            self.speaker_box.addItem(want, want)
        self.speaker_box.setCurrentIndex(max(0, self.speaker_box.findData(want)))

    def _style(self) -> dict[str, Any]:
        return {"speaker": self.speaker_box.currentData() or "" if self.speaker_row.isVisible() else "",
                "speed": round(self.v_speed.value(), 2), "pitch": round(self.v_pitch.value(), 1),
                "expressiveness": round(self.v_expr.value(), 2), "effect": self.v_effect.currentData() or "none",
                "blend": self.v_blend.currentData() or "", "blend_amount": round(self.v_blend_amount.value(), 2)}

    def test_persona(self) -> None:
        self.persona_btn.setEnabled(False)
        self.persona_result.setPlainText("Asking a few questions… (uses the local response model)")

        def done(res: Any) -> None:
            self.persona_btn.setEnabled(True)
            if not isinstance(res, dict):
                self.persona_result.setPlainText(str(res))
                return
            lines = [f"Error: {res['error']}"] if res.get("error") else \
                [f"Average {res.get('average')}/5 — {res.get('verdict')}", ""]
            for r in res.get("results", []):
                lines += [f"Q: {r['question']}", f"A: {r['reply']}", f"   {r['score']}/5 {r['why']}", ""]
            self.persona_result.setPlainText("\n".join(lines))
        self.daemon.call("persona.test", done, done, agent=self._collect())

    def preview(self) -> None:
        a = self._collect()
        self.preview_btn.setEnabled(False)
        self.voice_status.setText("Speaking…")

        def done(res: Any) -> None:
            self.preview_btn.setEnabled(True)
            if isinstance(res, dict):
                self.voice_status.setText("Played." if res.get("ok") else f"Couldn't play it: {res.get('error')}")
            else:
                self.voice_status.setText(str(res))
        self.daemon.call("voice.preview", done, done, agent=a)

    def _download_voice(self) -> None:
        m = self._voice_entry()
        if m:
            self.daemon.call("models.download", None, None, id=m["id"])
            self.voice_status.setText(f"Downloading {m['name']}… (progress on the Models page)")

    def select(self, aid: str | None) -> None:
        if self.autosave.pending() and aid != self.current:
            self.autosave.flush()                  # (the agent you were on keeps its changes)
        self.current = aid
        a = self.agents.get(aid or "")
        self.form.setEnabled(a is not None)
        if a is None:
            return
        i = self.picker.findData(aid)
        self.picker.setCurrentIndex(max(0, i))
        self.name.setText(a.get("name", ""))
        self.call_names.setText(", ".join(a.get("call_names", [])))
        self.threshold_global.setChecked(a.get("threshold") is None)
        self.threshold.setValue(a.get("threshold") or 0.6)
        self.jeenius.setCurrentIndex(max(0, self.jeenius.findData(int(a.get("jeenius") or 0))))
        self.commands.load(a.get("commands") or [])
        rud = a.get("rudimentary") or {}
        self.rud_on.setChecked(bool(rud.get("enabled")))
        self._fill_rud_functions(a, rud.get("function") or "")
        self.rud_program.setText(rud.get("program") or "")
        self.rud_confirm.setCurrentIndex(max(0, self.rud_confirm.findData(rud.get("confirm") or "risky")))
        self.rud_terminal.setChecked(bool(rud.get("terminal")))
        self._rudimentary(self.rud_on.isChecked())
        from ..functions.custom_sources import to_text
        cs = a.get("custom_sources") or {}
        self.sources_on.setChecked(bool(cs.get("enabled", True)))
        self.sources_edit.setEnabled(self.sources_on.isChecked())
        self.sources_edit.setPlainText(to_text(cs.get("sites") or []))
        reply = a.get("wake_reply") or ""
        self.wake_reply.setText(" | ".join(reply) if isinstance(reply, list) else str(reply))
        self.prompt.setPlainText(a.get("prompt", ""))
        self.persona.setCurrentIndex(max(0, self.persona.findData(a.get("persona_preset") or "")))
        self.strength.setCurrentIndex(max(0, self.strength.findData(a.get("persona_strength") or "strong")))
        self.persona_check.setChecked(bool(a.get("persona_check", False)))
        self.every_line.setChecked(bool(a.get("persona_every_line", True)))
        self.listen.setCurrentIndex(max(0, self.listen.findData(a.get("listen_to", "user"))))
        self._want_device = a.get("listen_device", "")
        self._fill_devices()
        self._device_visibility()
        j = a.get("jump_in") or {}
        self.jump_in.setChecked(bool(j.get("enabled")))
        self.jump_freq.setValue(float(j.get("frequency", 0.3)))
        self.jump_freq.setEnabled(bool(j.get("enabled")))
        m = a.get("memory") or {}
        self.mem_enabled.setChecked(bool(m.get("enabled", True)))
        self.mem_recent.setValue(-1 if m.get("recent") is None else int(m["recent"]))
        self.mem_notes.setValue(int(m.get("notes", 30) if m.get("notes") is not None else 30))
        self.mem_own.setChecked(bool(m.get("own_only", False)))
        self._memory_enabled(self.mem_enabled.isChecked())
        self.output.setCurrentIndex(max(0, self.output.findData(a.get("output_to", "speakers"))))
        self._want_out_device = a.get("output_device", "")
        self._fill_out_devices()
        self._device_visibility()
        self.show_output.setChecked(a.get("show_output", True))
        m = a.get("mute") or {}
        self.mute_audio.setChecked(bool(m.get("audio")) or bool((a.get("rudimentary") or {}).get("mute")))
        self.mute_visual.setChecked(bool(m.get("visual")))
        self.color_edit.setText(a.get("indicator_color") or "")
        self.enable_open.setText(", ".join(a.get("enable_when_open", [])))
        self.enable_focus.setText(", ".join(a.get("enable_when_focused", [])))
        self.disable_open.setText(", ".join(a.get("disable_when_open", [])))
        self.disable_focus.setText(", ".join(a.get("disable_when_focused", [])))
        for kind, cbox in self.model_boxes.items():
            if kind != "tts_voice":
                cbox.setCurrentIndex(max(0, cbox.findData((a.get("models") or {}).get(kind))))
        vs = dict(a.get("voice_style") or {})
        self._want_speaker = str(vs.get("speaker") or "")
        self.v_speed.setValue(float(vs.get("speed", 1.0)))
        self.v_pitch.setValue(float(vs.get("pitch", 0.0)))
        self.v_expr.setValue(float(vs.get("expressiveness", 0.667)))
        self.v_effect.setCurrentIndex(max(0, self.v_effect.findData(vs.get("effect", "none"))))
        self.v_blend.setCurrentIndex(max(0, self.v_blend.findData(vs.get("blend", ""))))
        self.v_blend_amount.setValue(float(vs.get("blend_amount", 0.3)))
        self._fill_voices(want=(a.get("models") or {}).get("tts_voice"))
        per = a.get("functions") or {}
        for f in self.functions:
            cb = self.func_checks.get(f["name"])
            if cb:
                cb.setChecked(bool(per.get(f["name"], f["globally_enabled"])))
        for cb in self.handoff_checks.values():
            discard(cb)
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

        QTimer.singleShot(0, self._settled)

    def _settled(self) -> None:
        """The form shows the agent as saved: new inputs are watched, and this is the baseline."""
        self.autosave.watch()
        if not self.autosave.pending():         # (an edit waiting to be saved isn't forgotten)
            self.autosave.loaded()

    def _sources_changed(self) -> None:
        from ..functions.custom_sources import parse
        sites, problems = parse(self.sources_edit.toPlainText())
        n = len(sites)
        self.sources_status.setText("; ".join(problems).capitalize() if problems else
                                    f"{n} site{'s' if n != 1 else ''}." if n else "")

    def _rudimentary(self, on: bool) -> None:
        """A rudimentary agent has just its one function: the function list doesn't apply (its own
        commands do: one of them can be its function)."""
        from ..functions.rudimentary import NAME
        self.rud_function.setEnabled(on)
        self.rud_cmd_box.setVisible(on and self.rud_function.currentData() == NAME)
        self.func_box.setEnabled(not on)

    def _fill_rud_functions(self, agent: dict[str, Any] | None = None, want: str | None = None) -> None:
        """Every full function, and this agent's own commands."""
        from ..functions.commands import function_name
        if want is None:
            want = self.rud_function.currentData() or ""
        agent = agent if agent is not None else self.agents.get(self.current or "", {})
        taken = {f["name"] for f in self.functions}
        items = [(f"{f['title']}", f["name"]) for f in sorted(self.functions, key=lambda f: f["title"])]
        for spec in agent.get("commands") or []:
            if isinstance(spec, dict) and spec.get("name") and spec.get("command"):
                name = function_name(spec["name"], taken)
                taken.add(name)
                items.append((f"{spec['name']} (its command)", name))
        self.rud_function.blockSignals(True)
        self.rud_function.clear()
        from ..functions.rudimentary import NAME
        self.rud_function.addItem("— pick one —", "")
        self.rud_function.addItem("Run commands (what I say is the command)", NAME)
        for text, key in items:
            self.rud_function.addItem(text, key)
        if want and self.rud_function.findData(want) < 0:
            self.rud_function.addItem(f"{want} (not found)", want)
        self.rud_function.setCurrentIndex(max(0, self.rud_function.findData(want)))
        self.rud_function.blockSignals(False)

    def _memory_enabled(self, on: bool) -> None:
        for w in (self.mem_recent, self.mem_notes, self.mem_own):
            w.setEnabled(on)

    def _device_visibility(self) -> None:
        on = self.listen.currentData() == "device"
        self.device_row.setVisible(on)
        self.device_hint.setVisible(on)
        self.out_device_row.setVisible(self.output.currentData() in ("device", "device_mic"))

    def _fill_out_devices(self) -> None:
        want = getattr(self, "_want_out_device", "")
        self.out_device.clear()
        for d in getattr(self, "outputs", []):
            self.out_device.addItem(d["description"], d["name"])
        if want and self.out_device.findData(want) < 0:
            self.out_device.addItem(f"{want}  (not connected)", want)
        self.out_device.setCurrentIndex(max(0, self.out_device.findData(want)))

    def _fill_devices(self) -> None:
        want = getattr(self, "_want_device", "")
        self.device.clear()
        for d in getattr(self, "devices", []):
            kind = "output" if d["kind"] == "output" else "mic"
            self.device.addItem(f"{d['description']}  ({kind})", d["name"])
        if want and self.device.findData(want) < 0:
            self.device.addItem(f"{want}  (not connected)", want)
        self.device.setCurrentIndex(max(0, self.device.findData(want)))

    def _got_devices(self, res: Any) -> None:
        self._loaded_parts.add("devices")
        QTimer.singleShot(0, self._settled)
        self.devices = (res or {}).get("devices", [])
        self.outputs = (res or {}).get("outputs", [])
        self._fill_devices()
        self._fill_out_devices()

    def _collect(self) -> dict[str, Any]:
        a = copy.deepcopy(self.agents.get(self.current or "", {}))
        split = lambda w: [x.strip() for x in w.text().split(",") if x.strip()]  # noqa: E731
        a.update({
            "name": self.name.text().strip() or self.current,
            "call_names": split(self.call_names) or [self.name.text().strip() or self.current],
            "threshold": None if self.threshold_global.isChecked() else round(self.threshold.value(), 2),
            "wake_reply": " | ".join(p.strip() for p in self.wake_reply.text().split("|") if p.strip()),
            "prompt": self.prompt.toPlainText(), "persona_check": self.persona_check.isChecked(),
            "persona_every_line": self.every_line.isChecked(),
            "persona_preset": self.persona.currentData() or None, "persona_strength": self.strength.currentData(),
            "listen_to": self.listen.currentData(),
            "listen_device": self.device.currentData() or "" if self.listen.currentData() == "device" else
            a.get("listen_device", ""),
            "jump_in": {"enabled": self.jump_in.isChecked(), "frequency": round(self.jump_freq.value(), 2)},
            "memory": {"enabled": self.mem_enabled.isChecked(),
                       "recent": None if self.mem_recent.value() < 0 else self.mem_recent.value(),
                       "notes": self.mem_notes.value(), "own_only": self.mem_own.isChecked()},
            "output_to": self.output.currentData(), "show_output": self.show_output.isChecked(),
            "mute": {"audio": self.mute_audio.isChecked(), "visual": self.mute_visual.isChecked()},
            "output_device": self.out_device.currentData() or "" if self.output.currentData() in
            ("device", "device_mic") else a.get("output_device", ""),
            "indicator_color": self.color_edit.text().strip() or None,
            "enable_when_open": split(self.enable_open), "enable_when_focused": split(self.enable_focus),
            "disable_when_open": split(self.disable_open), "disable_when_focused": split(self.disable_focus),
            "models": {k: c.currentData() for k, c in self.model_boxes.items()},
            "voice_style": self._style(),
            "functions": {name: cb.isChecked() for name, cb in self.func_checks.items()},
            "commands": self.commands.value(),
            "rudimentary": {"enabled": self.rud_on.isChecked(), "function": self.rud_function.currentData() or "",
                            "mute": False, "program": self.rud_program.text().strip(),
                            "confirm": self.rud_confirm.currentData() or "risky",
                            "terminal": self.rud_terminal.isChecked()},
            "jeenius": self.jeenius.currentData() or None,
            "custom_sources": {"enabled": self.sources_on.isChecked(),
                               "sites": parse_sources(self.sources_edit.toPlainText())[0]},
            "handoff_to": ["*"] if self.any_handoff.isChecked() else
            [k for k, cb in self.handoff_checks.items() if cb.isChecked()],
        })
        return a

    def save(self) -> None:
        if not self.current:
            return
        self.autosave.timer.stop()
        self.daemon.call("agents.save", lambda _r: show_message(self, "Agents", "Saved."), None,
                         id=self.current, agent=self._collect())
        self.autosave.loaded()

    def _save_quiet(self) -> None:
        """The autosave: no "Saved." box, a note beside the Save button instead."""
        aid, agent = self.current, self._collect()
        self._autosaved_at = self._now()

        def done(_r: Any) -> None:
            import time
            self.saved_note.setText(time.strftime("Saved automatically %H:%M:%S"))
        self.daemon.call("agents.save", done, lambda e: show_message(self, "Agents", f"Couldn't save: {e}"),
                         id=aid, agent=agent)

    def hideEvent(self, ev: Any) -> None:
        if self.autosave.pending():
            self.autosave.flush()              # (leaving the page: what's pending is saved first)
        super().hideEvent(ev)

    def add_agent(self) -> None:
        name, ok = ask_text(self, "New agent", "Its name (also the first name you call it by):",
                            placeholder="Jarvis")
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
