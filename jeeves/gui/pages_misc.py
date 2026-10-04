"""General, Indicators, Accounts, History, Dry Run, Training, Wikipedia and
Appearance pages."""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QListWidget, QPlainTextEdit, QVBoxLayout, QWidget

from .common import JEEVES_THEME_DEFAULTS
from .ui_kit import (CustomButton, CustomLineEdit, ThemeEditorGroup, ThemeSettings,
                     get_settings, show_message)
from .widgets import discard, Binder, Page, combo, label, row


def combo_text(keys: list[str] | None) -> str:
    return " + ".join(keys or [])


# ---------------------------------------------------------------------------
class GeneralPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Listening & Keys", "How long Jeeves listens, keybinds, audio devices, and safety "
                                             "settings for running commands.")
        self.daemon = daemon
        self.b = Binder(send)
        s = self.section("Listening")
        self.b.number(s, "Stop listening after this much silence", "general.end_of_speech_seconds", 0.3, 10, 0.1, 1,
                      suffix=" s")
        self.b.number(s, "Clicking the onscreen mic adds", "general.mic_click_extend_seconds", 1, 60, 1, 0, suffix=" s")
        self.b.number(s, "After letting go of a held mic, keep listening", "general.mic_hold_release_seconds",
                      0, 10, 0.5, 1, suffix=" s")
        self.b.number(s, "Longest single request", "general.max_request_seconds", 5, 600, 5, 0, suffix=" s")
        self.b.number(s, "Below this confidence, ask a clarifying question", "general.unclear_confidence",
                      0, 1, 0.05, 2)
        self.b.number(s, "Speech detection level", "audio.vad_threshold", 0.001, 0.2, 0.002, 3,
                      hint="Raise it if background noise keeps Jeeves listening; lower it if quiet speech is missed.")

        k = self.section("Keybinds")
        k.addWidget(label("Key names are Linux names joined with +, e.g. KEY_LEFTMETA+KEY_J. Keybinds need read "
                          "access to the keyboard (the NixOS module grants it). You can also bind these commands "
                          "in your desktop's shortcut settings instead: jeeves --manual_request=text · "
                          "jeeves --manual_request=voice --agent=NAME · jeeves --review · jeeves --abort"))
        self.b.check(k, "Watch the keyboard for keybinds", "general.watch_keyboard_for_keybinds")
        self.b.check(k, "Always listen for wake words", "wake_word.enabled",
                     hint="Off: the microphone is only open during a Voice Request or while an agent waits for "
                          "your answer. Voice Request: Unknown then works like a push-to-talk wake word.")
        self.combo_edit(k, "Abort (stops all agents, releases all inputs)", "general.abort_key", single=True)
        self.b.check(k, "Text Request keybind", "manual_request.text_keybind_enabled")
        self.combo_edit(k, "Text Request", "manual_request.text_keybind")
        self.b.check(k, "Voice Request keybinds", "manual_request.voice_keybind_enabled")
        self.voice_rows = QWidget()
        self.voice_layout = QVBoxLayout(self.voice_rows)
        self.voice_layout.setContentsMargins(0, 0, 0, 0)
        k.addWidget(self.voice_rows)
        self.combo_edit(k, "Manual Response Review", "manual_request.review_keybind")
        self.combo_edit(k, "Turn Jeeves on/off (all AIs)", "manual_request.toggle_keybind")

        a = self.section("Audio devices")
        self.mic = self.b.choice(a, "Microphone", "audio.microphone", [("Default", "")])
        self.desk = self.b.choice(a, "Desktop audio", "audio.desktop", [("Default output's monitor", "@DEFAULT_MONITOR@")])
        self.spk = self.b.choice(a, "Speakers", "audio.speaker", [("Default", "")])
        self.b.text(a, "Virtual microphone name", "audio.virtual_mic_sink",
                    hint="Agents set to speak 'through the microphone' play into this; pick 'Jeeves-Microphone' "
                         "as the mic in Discord/OBS.")

        r = self.section("Run Command safety")
        self.b.check(r, "Show commands and wait for confirmation before running them", "run_command.confirm")
        self.b.text(r, "Confirm keyword", "run_command.confirm_keyword")
        self.b.list_text(r, "Trusted commands (no confirmation)", "run_command.trusted", sep=";",
                         hint="Separate with ;. A command with no arguments trusts every argument list "
                              "('puppetry'); with arguments, only exactly those ('puppetry --list'). Shell syntax "
                              "(; && | $() > <) is always rejected.")
        self.b.number(r, "Stop commands after", "run_command.timeout_seconds", 1, 3600, 5, 0, suffix=" s")

        m = self.section("Memory & Summary")
        self.b.number(m, "Recent requests remembered", "memory.recent_count", 0, 50, 1)
        self.b.number(m, "Long-term (RAM) notes kept", "memory.long_term_limit", 10, 10000, 10)
        self.b.check(m, "Summary log (keeps a transcript of what's heard)", "summary.enabled",
                     hint="Mic and desktop audio are kept separate with timestamps. While on, the wake word "
                          "model is off.")
        self.b.number(m, "Keep the last", "summary.minutes", 1, 1440, 5, 0, suffix=" min")
        self.b.list_text(m, "Sources", "summary.sources", placeholder="microphone, desktop")
        self.memory_view = QPlainTextEdit()
        self.memory_view.setReadOnly(True)
        self.memory_view.setMaximumHeight(140)
        m.addWidget(label("Remembered notes"))
        m.addWidget(self.memory_view)

        c = self.section("Control Mode & Puppetry")
        self.b.check(c, "Create a virtual controller (jeeves-controller)", "control_mode.virtual_controller")
        self.b.text(c, "Puppetry config folder", "puppetry.config_dir")
        self.b.text(c, "Puppetry service", "puppetry.service")
        self.finish()

    def combo_edit(self, layout: QVBoxLayout, text: str, path: str, single: bool = False) -> None:
        e = CustomLineEdit()

        def out() -> None:
            keys = [x.strip().upper() for x in e.text().split("+") if x.strip()]
            self.b._emit(path, (keys[0] if keys else None) if single else keys)
        e.editingFinished.connect(out)
        self.b.form_row(layout, text, e, path,
                        lambda v: e.setText(v if isinstance(v, str) else combo_text(v)))

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.settings = settings
        for i in reversed(range(self.voice_layout.count())):
            w = self.voice_layout.itemAt(i).widget()
            if w:
                discard(w)
        self.b.items = [it for it in self.b.items if not it[0].startswith("manual_request.voice_keybinds.")]
        self.combo_edit(self.voice_layout, "Voice Request: Unknown (say the agent's name)",
                        "manual_request.voice_keybinds._unknown")
        for aid, a in (settings.get("agents") or {}).items():
            self.combo_edit(self.voice_layout, f"Voice Request: {a.get('name', aid)}",
                            f"manual_request.voice_keybinds.{aid}")

        def devices(d: Any) -> None:
            devs = d.get("devices") or [{"name": n, "description": n,
                                         "kind": "output" if n.endswith(".monitor") else "microphone"}
                                        for n in d.get("sources", [])]
            mics = [(x["description"], x["name"]) for x in devs if x["kind"] == "microphone"]
            outs = [(f"{x['description']}", x["name"]) for x in devs if x["kind"] == "output"]
            for box, items, first in ((self.mic, mics, ("Default", "")),
                                      (self.desk, outs, ("Default output's monitor", "@DEFAULT_MONITOR@")),
                                      (self.spk, [(n, n) for n in d.get("sinks", [])], ("Default", ""))):
                box.clear()
                box.addItem(*first)
                for text, value in items:
                    box.addItem(text, value)
            self.b.load(settings, locked)
        self.daemon.call("audio.devices", devices, lambda _e: self.b.load(settings, locked))
        self.daemon.call("memory.list", lambda notes: self.memory_view.setPlainText(
            "\n".join(("[forever] " if n.get("permanent") else "") + n["text"] for n in (notes or []))), lambda _e: None)
        self.b.load(settings, locked)


# ---------------------------------------------------------------------------
class IndicatorsPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Indicators", "What appears on screen while agents work. Click the mic to listen 5 s "
                                       "longer, hold it to keep listening. Click a spinner to see the agent's "
                                       "thoughts, pause a response, or confirm.")
        self.b = Binder(send)
        s = self.section("Show")
        self.b.check(s, "Microphone while listening", "indicators.stt_active")
        self.b.check(s, "What you said (transcript), under the mic", "indicators.stt_output")
        self.b.check(s, "Processing spinner", "indicators.processing")
        self.b.check(s, "Response text", "indicators.response_text")
        self.b.check(s, "Timers (bottom right)", "indicators.timers")
        self.b.choice(s, "Corner", "indicators.corner", [("Top right", "top-right"), ("Top left", "top-left"),
                                                         ("Bottom right", "bottom-right"),
                                                         ("Bottom left", "bottom-left")])
        from PySide6.QtGui import QGuiApplication
        screens = [(f"{sc.name()} ({sc.geometry().width()}×{sc.geometry().height()})", sc.name())
                   for sc in QGuiApplication.screens()]
        self.b.choice(s, "Screen", "indicators.screen",
                      [("The one the mouse is on", "mouse"), ("Primary screen", "primary")] + screens,
                      hint="Where indicators appear. 'The one the mouse is on' is picked each time an indicator "
                           "appears (needs kdotool on KDE, which the NixOS package includes).")
        self.b.number(s, "Size", "indicators.size", 24, 160, 4, 0, suffix=" px")
        c = self.section("Colors")
        for key, title in (("thinking", "Thinking"), ("researching", "Researching"), ("responding", "Responding"),
                           ("responding_outline", "Responding outline"), ("asking", "Asking for input (flashes)"),
                           ("unclear", "Unclear"), ("unavailable", "Model unavailable (flashes)")):
            self.b.color(c, title, f"indicators.colors.{key}")
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.b.load(settings, locked)


# ---------------------------------------------------------------------------
class AccountsPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Accounts", "Online AIs for Online Prompt Mode. GPT goes through the Codex CLI (sign in "
                                     "with your ChatGPT account, free tier included). Gemini uses a free Google AI "
                                     "Studio API key. Claude and Grok have no free programmatic access, so they "
                                     "open in the Jeeves browser: you press send and the site's copy button, and "
                                     "Jeeves reads the copied answer.")
        self.daemon = daemon
        self.b = Binder(send)
        self.status_labels: dict[str, QLabel] = {}
        g = self.section("GPT (Codex CLI)")
        self.b.check(g, "Enabled", "accounts.codex.enabled")
        self.b.text(g, "Codex command", "accounts.codex.binary")
        self._status_row(g, "codex", "Sign in (opens a terminal)")
        g = self.section("Gemini")
        self.b.check(g, "Enabled", "accounts.gemini.enabled")
        self.b.text(g, "Model", "accounts.gemini.model")
        self.key = CustomLineEdit()
        self.key.setEchoMode(CustomLineEdit.Password)
        self.key.setPlaceholderText("paste your API key (stored in ~/.config/jeeves/secrets.json, mode 600)")
        save_key = CustomButton("Save key")
        save_key.clicked.connect(lambda: daemon.call("accounts.set_key", lambda _r: self.refresh_status(), None,
                                                     name="gemini_api_key", value=self.key.text().strip() or None))
        g.addWidget(row(self.key, save_key, stretch_last=False))
        self._status_row(g, "gemini", None)
        for site in ("claude", "grok"):
            g = self.section(site.title() + " (Jeeves browser)")
            self.b.check(g, "I've logged in to the Jeeves browser", f"accounts.{site}.enabled")
            self._status_row(g, site, "Open the Jeeves browser to log in")
        g = self.section("Jeeves browser & defaults")
        self.b.text(g, "Browser command", "accounts.browser.binary", placeholder="auto (chromium, chrome, brave, firefox)")
        self.b.number(g, "Wait for the copied answer up to", "accounts.browser.clipboard_timeout_seconds", 10, 3600,
                      10, 0, suffix=" s")
        self.b.choice(g, "Default online AI", "functions.online_agent",
                      [("GPT (Codex)", "codex"), ("Gemini", "gemini"), ("Claude", "claude"), ("Grok", "grok")])
        g.addWidget(label("MCP: Codex uses the MCP servers in your Codex config (~/.codex/config.toml). "
                          "Gemini's API and the browser sites don't take tools from Jeeves."))
        self.finish()

    def _status_row(self, layout: QVBoxLayout, name: str, button: str | None) -> None:
        lab = QLabel("")
        lab.setWordWrap(True)
        self.status_labels[name] = lab
        widgets: list[QWidget] = [lab]
        if button:
            b = CustomButton(button)
            b.clicked.connect(lambda: self.daemon.call("accounts.login", None, None, name=name))
            widgets.append(b)
        layout.addWidget(row(*widgets, stretch_last=False))

    def refresh_status(self) -> None:
        def got(items: Any) -> None:
            for it in items or []:
                lab = self.status_labels.get(it["name"])
                if lab:
                    lab.setText(("✓ " if it["ready"] else "✗ ") + it["detail"])
        self.daemon.call("accounts.status", got, lambda _e: None)

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.b.load(settings, locked)
        self.refresh_status()


# ---------------------------------------------------------------------------
class HistoryPage(Page):
    def __init__(self, daemon: Any) -> None:
        super().__init__("History", "Rate responses so agents learn what you mean. Ratings, comments and the "
                                    "function you say it should have used are shown to the intent model next to "
                                    "each function in the Dictionary.")
        self.daemon = daemon
        self.items: list[dict[str, Any]] = []
        self.list = QListWidget()
        self.list.setMinimumHeight(240)
        self.list.currentRowChanged.connect(self.show_item)
        self.body_layout.addWidget(self.list)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(220)
        self.body_layout.addWidget(self.detail)
        rate = self.section("Rate this response")
        self.good = CustomButton("👍 Good")
        self.bad = CustomButton("👎 Bad")
        self.clear = CustomButton("Clear rating")
        self.should = combo()
        self.comment = CustomLineEdit()
        self.comment.setPlaceholderText("Why was it good or bad? (optional)")
        rate.addWidget(row(self.good, self.bad, self.clear))
        rate.addWidget(row(label("Should have used", False), self.should, stretch_last=True))
        rate.addWidget(self.comment)
        self.good.clicked.connect(lambda: self.rate(1))
        self.bad.clicked.connect(lambda: self.rate(-1))
        self.clear.clicked.connect(lambda: self.rate(None))
        refresh = CustomButton("Refresh")
        refresh.clicked.connect(lambda: self.refresh({}, set()))
        self.body_layout.addWidget(refresh, alignment=Qt.AlignRight)
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.daemon.call("history.list", self._got, lambda _e: None, limit=200)
        self.daemon.call("functions.list", self._funcs, lambda _e: None)

    def _funcs(self, funcs: Any) -> None:
        cur = self.should.currentData()
        self.should.clear()
        self.should.addItem("(the one it used)", None)
        for f in funcs or []:
            if f["kind"] == "full":
                self.should.addItem(f["title"], f["name"])
        self.should.setCurrentIndex(max(0, self.should.findData(cur)))

    def _got(self, items: Any) -> None:
        self.items = items or []
        row_now = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.items:
            mark = {1: "👍 ", -1: "👎 "}.get(e.get("rating"), "")
            self.list.addItem(f"{mark}{time.strftime('%m-%d %H:%M', time.localtime(e['time']))}  "
                              f"{e.get('agent')}: {e.get('text')}  →  {e.get('function')}")
        self.list.blockSignals(False)
        if self.items:
            self.list.setCurrentRow(max(0, min(row_now, len(self.items) - 1)))

    def show_item(self, i: int) -> None:
        if not (0 <= i < len(self.items)):
            return
        e = self.items[i]
        lines = [f"Request: {e.get('text')}", f"Agent: {e.get('agent')}   Source: {e.get('source')}",
                 f"Function: {e.get('function')} {json.dumps(e.get('args'))}   Status: {e.get('status')}",
                 f"Response: {e.get('response')}", "", "Trace:"]
        for t in e.get("trace", []):
            rest = {k: v for k, v in t.items() if k not in ("t", "kind")}
            lines.append(f"  {t.get('t', 0):6.2f}s  {t.get('kind'):<14} {json.dumps(rest, default=str)}")
        self.detail.setPlainText("\n".join(lines))
        self.comment.setText(e.get("comment") or "")
        self.should.setCurrentIndex(max(0, self.should.findData(e.get("should_use"))))

    def rate(self, value: int | None) -> None:
        i = self.list.currentRow()
        if not (0 <= i < len(self.items)):
            return
        self.daemon.call("history.rate", lambda _r: self.refresh({}, set()), None, id=self.items[i]["id"],
                         rating=value, comment=self.comment.text(), should_use=self.should.currentData())


# ---------------------------------------------------------------------------
class DryRunPage(Page):
    def __init__(self, daemon: Any) -> None:
        super().__init__("Dry Run", "See what WOULD happen if you said something: which agent, which function and "
                                    "arguments, and every step it would take. Nothing is run, spoken or changed.")
        self.daemon = daemon
        self.agent = combo()
        self.text = CustomLineEdit()
        self.text.setPlaceholderText("Jeeves, set a timer for ten minutes")
        self.text.returnPressed.connect(self.go)
        btn = CustomButton("Try it")
        btn.clicked.connect(self.go)
        self.body_layout.addWidget(row(label("Agent", False), self.agent, self.text, btn))
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMinimumHeight(400)
        self.body_layout.addWidget(self.out)
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        cur = self.agent.currentData()
        self.agent.clear()
        self.agent.addItem("(from the text)", None)
        for aid, a in (settings.get("agents") or {}).items():
            self.agent.addItem(a.get("name", aid), aid)
        self.agent.setCurrentIndex(max(0, self.agent.findData(cur)))

    def go(self) -> None:
        text = self.text.text().strip()
        if not text:
            return
        self.out.setPlainText("Thinking…")

        def done(e: Any) -> None:
            lines = [f"Agent:      {e.get('agent')}", f"Request:    {e.get('text')}",
                     f"Function:   {e.get('function')}", f"Arguments:  {json.dumps(e.get('args'), indent=1)}",
                     f"Confidence: {e.get('confidence')}", "", "Steps:"]
            for t in e.get("trace", []):
                if t["kind"] == "stage":
                    continue
                rest = {k: v for k, v in t.items() if k not in ("t", "kind")}
                lines.append(f"  {t['kind']:<14} {json.dumps(rest, default=str)}")
            if e.get("response"):
                lines += ["", f"Would say: {e['response']}"]
            self.out.setPlainText("\n".join(lines))
        self.daemon.call("request.dry_run", done, lambda err: self.out.setPlainText(f"Error: {err}"),
                         text=text, agent=self.agent.currentData(), timeout=120)


# ---------------------------------------------------------------------------
class TrainingPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Training", "Teach speech recognition your voice and vocabulary, and teach the intent "
                                     "model what you mean.")
        self.daemon = daemon
        self.b = Binder(send)
        s = self.section("Your voice (speech to text)")
        s.addWidget(label("Read each phrase after pressing Record (listening stops when you stop talking). "
                          "Recordings are kept as a dataset; their words and your vocabulary bias recognition "
                          "toward names and jargon. Evaluate shows the error rate with and without that help."))
        self.phrases = QListWidget()
        self.phrases.setMaximumHeight(200)
        s.addWidget(self.phrases)
        rec = CustomButton("Record selected phrase")
        rec.clicked.connect(self.record)
        ev = CustomButton("Evaluate")
        ev.clicked.connect(lambda: daemon.call("training.evaluate", lambda r: show_message(
            self, "Evaluation", json.dumps(r, indent=1)), None, timeout=600))
        exp = CustomButton("Export dataset…")
        exp.clicked.connect(self.export)
        self.count = QLabel("")
        s.addWidget(row(rec, ev, exp, self.count))
        self.b.list_text(s, "Extra vocabulary", "training.stt_vocabulary", placeholder="Afterglow, Puppetry, Hyprland")

        i = self.section("What you mean (intent)")
        self.b.check(i, "Show rated history to the intent model", "training.intent_examples_from_ratings")
        self.b.number(i, "Rated examples shown", "training.max_examples", 0, 100, 1)
        i.addWidget(label("Training phrases: when you say this, run that function."))
        self.ip_text = CustomLineEdit()
        self.ip_text.setPlaceholderText("clip that")
        self.ip_func = combo()
        self.ip_args = CustomLineEdit()
        self.ip_args.setPlaceholderText('{"seconds": 30}  (optional JSON)')
        add = CustomButton("Add")
        add.clicked.connect(self.add_phrase)
        i.addWidget(row(self.ip_text, self.ip_func, self.ip_args, add))
        self.ip_list = QListWidget()
        self.ip_list.setMaximumHeight(180)
        i.addWidget(self.ip_list)
        rm = CustomButton("Remove selected")
        rm.clicked.connect(self.remove_phrase)
        i.addWidget(rm, alignment=Qt.AlignRight)
        self._intent: list[dict[str, Any]] = []
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.b.load(settings, locked)
        self.daemon.call("training.phrases", lambda ps: (self.phrases.clear(), self.phrases.addItems(ps or [])),
                         lambda _e: None)
        self.daemon.call("training.recordings", lambda rs: self.count.setText(f"{len(rs or [])} recordings"),
                         lambda _e: None)
        self.daemon.call("functions.list", self._funcs, lambda _e: None)
        self.daemon.call("training.intent_list", self._intents, lambda _e: None)

    def _funcs(self, funcs: Any) -> None:
        self.ip_func.clear()
        for f in funcs or []:
            if f["kind"] == "full":
                self.ip_func.addItem(f["title"], f["name"])

    def _intents(self, items: Any) -> None:
        self._intent = items or []
        self.ip_list.clear()
        for it in self._intent:
            self.ip_list.addItem(f"“{it['text']}” → {it['function']} {json.dumps(it.get('args') or {})}")

    def record(self) -> None:
        item = self.phrases.currentItem()
        if item is None:
            show_message(self, "Training", "Pick a phrase first.")
            return
        self.daemon.call("training.record", None, None, text=item.text())

    def export(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "Export dataset to")
        if d:
            self.daemon.call("training.export", lambda p: show_message(self, "Exported", p), None, dest=d)

    def add_phrase(self) -> None:
        try:
            args = json.loads(self.ip_args.text()) if self.ip_args.text().strip() else {}
        except ValueError:
            show_message(self, "Training", "Arguments must be JSON")
            return
        self.daemon.call("training.intent_add", lambda _r: self.refresh({}, set()), None,
                         text=self.ip_text.text().strip(), function=self.ip_func.currentData(), args=args)

    def remove_phrase(self) -> None:
        i = self.ip_list.currentRow()
        if 0 <= i < len(self._intent):
            self.daemon.call("training.intent_remove", lambda _r: self.refresh({}, set()), None,
                             id=self._intent[i]["id"])

    def on_event(self, topic: str, data: Any) -> None:
        if topic == "training_recording":
            self.daemon.call("training.recordings", lambda rs: self.count.setText(f"{len(rs or [])} recordings"),
                             lambda _e: None)


# ---------------------------------------------------------------------------
class WikipediaPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Wikipedia", "Download all of Wikipedia (text only, no pictures) for agents to look things "
                                      "up offline. Re-run the download any time to get the latest edition.")
        self.daemon = daemon
        self.b = Binder(send)
        s = self.section("Offline copy")
        self.state = label("")
        s.addWidget(self.state)
        self.b.text(s, "Edition", "wikipedia.variant",
                    hint="wikipedia_en_all_nopic is all English articles without pictures (~50 GB). "
                         "wikipedia_en_top_nopic is the most-read articles only (a few GB).")
        self.b.text(s, "Mirror", "wikipedia.mirror")
        self.b.check(s, "Smart Update", "wikipedia.smart_update",
                     hint="Skips the download when there's no newer edition and resumes interrupted downloads. "
                          "Wikipedia's offline format has no change-only updates, so a newer edition is a full "
                          "download; the old copy stays usable until it finishes.")
        go = CustomButton("Download / Update")
        go.clicked.connect(lambda: daemon.call("wikipedia.update", None, None))
        stop = CustomButton("Cancel")
        stop.clicked.connect(lambda: daemon.call("wikipedia.cancel", None, None))
        s.addWidget(row(go, stop))
        t = self.section("Try a lookup")
        self.q = CustomLineEdit()
        self.q.setPlaceholderText("Alan Turing")
        self.q.returnPressed.connect(self.search)
        t.addWidget(self.q)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(200)
        t.addWidget(self.result)
        self.finish()

    def search(self) -> None:
        self.daemon.call("wikipedia.search", lambda r: self.result.setPlainText(
            f"{r['title']}\n\n{r['text']}" if r else "Nothing found."),
            lambda e: self.result.setPlainText(e), query=self.q.text())

    def _show(self, st: Any) -> None:
        if not st:
            return
        p = st.get("progress") or {}
        txt = f"Installed: {st.get('file') or 'nothing yet'}"
        if st.get("size"):
            txt += f" ({st['size'] / 1e9:.1f} GB)"
        if not st.get("libzim"):
            txt += "  ·  python-libzim missing (needed to read it)"
        if p.get("state") == "downloading" and p.get("total"):
            txt += f"\nDownloading {p.get('file')}: {100 * p['done'] / p['total']:.1f}%"
        elif p.get("state") not in (None, "idle"):
            txt += f"\n{p.get('state')}{': ' + p['error'] if p.get('error') else ''}"
        self.state.setText(txt)

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.b.load(settings, locked)
        self.daemon.call("wikipedia.status", self._show, lambda _e: None)

    def on_event(self, topic: str, data: Any) -> None:
        if topic == "wikipedia":
            self._show(data)


# ---------------------------------------------------------------------------
class AppearancePage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Appearance", "Colors and shapes of this window and Jeeves' popups.")
        self.daemon, self.send = daemon, send
        self.editor = ThemeEditorGroup(current=get_settings(), defaults=JEEVES_THEME_DEFAULTS)
        self.body_layout.addWidget(self.editor)
        save = CustomButton("Save appearance")
        save.clicked.connect(self.save)
        self.body_layout.addWidget(save, alignment=Qt.AlignRight)
        self.finish()

    def save(self) -> None:
        target = ThemeSettings(**asdict(get_settings()))
        bad = self.editor.apply_to(target)
        msg = "Saved. Reopen Jeeves to apply everywhere."
        if bad:
            msg += "\n\nIgnored invalid colors: " + ", ".join(bad)
        self.daemon.call("settings.set", lambda _r: show_message(self, "Appearance", msg), None,
                         changes={"theme": asdict(target)})


__all__ = ["GeneralPage", "IndicatorsPage", "AccountsPage", "HistoryPage", "DryRunPage", "TrainingPage",
           "WikipediaPage", "AppearancePage"]
