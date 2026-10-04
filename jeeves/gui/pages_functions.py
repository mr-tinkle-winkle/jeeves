"""Functions page (the Dictionary): every function, its keywords and blocked
statements, plus creating, exporting and importing custom functions."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from .ui_kit import (CollapseToggleButton, CustomButton, CustomCheckBox, CustomGroupBox, CustomLineEdit,
                     show_message)
from .widgets import Page, combo, label, row

EXAMPLE_STEPS = """[
  {"call": "get_open_apps", "as": "apps"},
  {"if": "\\"obs\\" in apps", "then": [
      {"call": "speak", "args": {"text": "OBS is already open."}}
  ], "else": [
      {"call": "run_command", "args": {"command": "obs"}},
      {"call": "speak", "args": {"text": "Opening OBS."}}
  ]}
]"""


class FunctionRow(QWidget):
    def __init__(self, page: "FunctionsPage", f: dict[str, Any]) -> None:
        super().__init__()
        self.page, self.f = page, f
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.toggle = CollapseToggleButton(expanded=False)
        self.toggle.toggled.connect(self._expand)
        sig = ", ".join(a["name"] for a in f["args"])
        title = label(f"<b>{f['name']}</b>({sig})  <i>{f['kind']}{'' if f['source'] == 'builtin' else ' · ' + f['source']}</i>"
                      f"<br>{f['description']}")
        head.addWidget(self.toggle, 0, Qt.AlignTop)
        head.addWidget(title, 1)
        if f["kind"] == "full":
            self.enabled = CustomCheckBox("On")
            self.enabled.setChecked(f["globally_enabled"])
            self.enabled.toggled.connect(lambda v: page.send({f"functions.global_enabled.{f['name']}": bool(v)}))
            head.addWidget(self.enabled, 0, Qt.AlignTop)
        lay.addLayout(head)
        self.details = QWidget()
        d = QVBoxLayout(self.details)
        d.setContentsMargins(36, 0, 0, 8)
        if f["how"]:
            d.addWidget(label(f"How it works: {f['how']}"))
        for a in f["args"]:
            kind = f"one of {a['choices']}" if a.get("choices") else a["type"]
            req = "required" if a["required"] else f"optional (default {a['default']!r})"
            d.addWidget(label(f"• {a['name']}: {kind}, {req}. {a['description']}"))
        if f["returns"]:
            d.addWidget(label(f"Returns: {f['returns']}"))
        for ex in f["examples"]:
            d.addWidget(label(f"Example: {ex}"))
        self.kw = CustomLineEdit(", ".join(f["effective_keywords"]))
        self.kw.editingFinished.connect(self._save_keywords)
        d.addWidget(row(label("Keywords", False), self.kw, stretch_last=True))
        user_blocked = [b for b in f["effective_blocked"] if b not in f["blocked"]]
        self.blocked = CustomLineEdit("; ".join(user_blocked))
        self.blocked.setPlaceholderText("statements this function must never be used for (separate with ;)")
        self.blocked.editingFinished.connect(self._save_blocked)
        d.addWidget(row(label("Blocked statements", False), self.blocked, stretch_last=True))
        if f["blocked"]:
            d.addWidget(label("Built-in blocked: " + "; ".join(f["blocked"])))
        if f.get("steps") is not None:
            steps = QPlainTextEdit(json.dumps(f["steps"], indent=2))
            steps.setReadOnly(True)
            steps.setMinimumHeight(120)
            d.addWidget(steps)
        if f["source"] == "user" or f["source"].startswith("app:"):
            btns = QHBoxLayout()
            if f.get("steps") is not None and f["source"] == "user":
                edit = CustomButton("Edit")
                edit.clicked.connect(lambda: page.edit_function(f))
                btns.addWidget(edit)
            delete = CustomButton("Delete")
            delete.clicked.connect(lambda: page.delete_function(f["name"]))
            btns.addWidget(delete)
            btns.addStretch(1)
            d.addLayout(btns)
        self.details.setVisible(False)
        lay.addWidget(self.details)

    def _expand(self, on: bool) -> None:
        self.details.setVisible(on)

    def _save_keywords(self) -> None:
        vals = [x.strip() for x in self.kw.text().split(",") if x.strip()]
        self.page.send({f"functions.keywords.{self.f['name']}": vals})

    def _save_blocked(self) -> None:
        vals = [x.strip() for x in self.blocked.text().split(";") if x.strip()]
        self.page.send({f"functions.blocked.{self.f['name']}": vals})


class FunctionsPage(Page):
    def __init__(self, daemon: Any, send: Any) -> None:
        super().__init__("Functions", "The Dictionary the intent model reads. Change any keyword, add blocked "
                                      "statements, and build your own full functions out of partial functions.")
        self.daemon, self.send = daemon, send
        bar = QHBoxLayout()
        self.search = CustomLineEdit()
        self.search.setPlaceholderText("Search functions")
        self.search.textChanged.connect(self._filter)
        self.kind = combo()
        for lab, v in (("All", None), ("Full functions", "full"), ("Partial functions", "partial"),
                       ("Custom", "custom")):
            self.kind.addItem(lab, v)
        self.kind.activated.connect(lambda _i: self._filter())
        new = CustomButton("+ New function")
        new.clicked.connect(lambda: self.edit_function(None))
        imp = CustomButton("Import…")
        imp.clicked.connect(self.import_file)
        exp = CustomButton("Export custom…")
        exp.clicked.connect(self.export_file)
        rel = CustomButton("Reload")
        rel.clicked.connect(lambda: daemon.call("functions.reload", self._reloaded, None))
        for w in (self.search, self.kind, new, imp, exp, rel):
            bar.addWidget(w, 1 if w is self.search else 0)
        self.body_layout.addLayout(bar)
        self.problems = label("")
        self.body_layout.addWidget(self.problems)

        self.editor_box = CustomGroupBox("Custom function")
        e = self.editor_box.make_layout(QVBoxLayout)
        self.ed_name = CustomLineEdit()
        self.ed_name.setPlaceholderText("open_obs (lower case, underscores)")
        self.ed_desc = CustomLineEdit()
        self.ed_desc.setPlaceholderText("What it does -- the intent model reads this")
        self.ed_kw = CustomLineEdit()
        self.ed_kw.setPlaceholderText("keywords, comma separated")
        self.ed_args = QPlainTextEdit()
        self.ed_args.setPlaceholderText('[{"name": "app", "type": "app", "description": "Which app"}]')
        self.ed_args.setMaximumHeight(90)
        self.ed_steps = QPlainTextEdit()
        self.ed_steps.setMinimumHeight(220)
        e.addWidget(row(label("Name", False), self.ed_name, stretch_last=True))
        e.addWidget(row(label("Description", False), self.ed_desc, stretch_last=True))
        e.addWidget(row(label("Keywords", False), self.ed_kw, stretch_last=True))
        e.addWidget(label("Arguments (JSON list; types: string, number, integer, boolean, duration, time, app, "
                          "agent, key, path, url, command, list, object)"))
        e.addWidget(self.ed_args)
        e.addWidget(label("Steps -- partial functions combined with control flow (call / set / if / repeat / while / "
                          "for_each / when / wait / return). ${name} reads a variable or argument."))
        e.addWidget(self.ed_steps)
        self.partials_hint = label("")
        e.addWidget(self.partials_hint)
        btns = QHBoxLayout()
        check = CustomButton("Check")
        check.clicked.connect(self._check)
        save = CustomButton("Save function")
        save.clicked.connect(self._save)
        cancel = CustomButton("Close")
        cancel.clicked.connect(lambda: self.editor_box.setVisible(False))
        btns.addStretch(1)
        for b in (check, cancel, save):
            btns.addWidget(b)
        e.addLayout(btns)
        self.editor_box.setVisible(False)
        self.body_layout.addWidget(self.editor_box)

        self.list = QWidget()
        self.list_layout = QVBoxLayout(self.list)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.addWidget(self.list)
        self.rows: list[FunctionRow] = []
        self.functions: list[dict[str, Any]] = []
        self._sig = None
        self.finish()

    def refresh(self, settings: dict[str, Any], locked: set[str]) -> None:
        self.daemon.call("functions.list", self._got, lambda _e: None)
        self.daemon.call("status", lambda st: self.problems.setText(
            ("Problems: " + "; ".join(st.get("problems", []))) if st and st.get("problems") else ""), lambda _e: None)

    def _got(self, funcs: Any) -> None:
        sig = json.dumps(funcs, sort_keys=True, default=str)
        if sig == self._sig:
            return          # nothing changed: skip the rebuild (UI guide pitfall 14)
        self._sig = sig
        self.functions = funcs or []
        for r in self.rows:
            r.setParent(None)
        self.rows = []
        for f in sorted(self.functions, key=lambda f: (f["kind"] != "full", f["source"] != "builtin", f["name"])):
            r = FunctionRow(self, f)
            self.rows.append(r)
            self.list_layout.addWidget(r)
        partials = sorted(f["name"] for f in self.functions if f["kind"] == "partial")
        self.partials_hint.setText("Partials you can call: " + ", ".join(partials))
        self._filter()

    def _filter(self) -> None:
        q = self.search.text().lower()
        kind = self.kind.currentData()
        for r in self.rows:
            f = r.f
            hay = (f["name"] + f["description"] + " ".join(f["effective_keywords"])).lower()
            ok = q in hay and (kind is None or (kind == "custom" and f["source"] != "builtin") or f["kind"] == kind)
            r.setVisible(ok)

    def _reloaded(self, res: Any) -> None:
        self._sig = None
        self.refresh({}, set())
        if res and res.get("problems"):
            show_message(self, "Functions", "Reloaded with problems:\n" + "\n".join(res["problems"]))

    # ---- editor -------------------------------------------------------
    def edit_function(self, f: dict[str, Any] | None) -> None:
        self.editor_box.setVisible(True)
        self.ed_name.setText(f["name"] if f else "")
        self.ed_name.setEnabled(f is None)
        self.ed_desc.setText(f["description"] if f else "")
        self.ed_kw.setText(", ".join(f["keywords"]) if f else "")
        self.ed_args.setPlainText(json.dumps(f["args"], indent=1) if f else "[]")
        self.ed_steps.setPlainText(json.dumps(f["steps"], indent=2) if f else EXAMPLE_STEPS)

    def _parsed(self) -> dict[str, Any] | None:
        try:
            args = json.loads(self.ed_args.toPlainText() or "[]")
            steps = json.loads(self.ed_steps.toPlainText())
        except ValueError as exc:
            show_message(self, "Functions", f"That isn't valid JSON: {exc}")
            return None
        return {"name": self.ed_name.text().strip(), "kind": "full", "description": self.ed_desc.text().strip(),
                "keywords": [k.strip() for k in self.ed_kw.text().split(",") if k.strip()], "args": args,
                "steps": steps, "category": "custom"}

    def _check(self) -> None:
        data = self._parsed()
        if data is None:
            return
        self.daemon.call("functions.validate", lambda probs: show_message(
            self, "Check", "Looks good." if not probs else "\n".join(probs)), None, steps=data["steps"])

    def _save(self) -> None:
        data = self._parsed()
        if data is None:
            return
        if not data["name"] or not data["description"]:
            show_message(self, "Functions", "A function needs a name and a description.")
            return

        def saved(_r: Any) -> None:
            self.editor_box.setVisible(False)
            self._sig = None
            self.refresh({}, set())
        self.daemon.call("functions.save", saved, None, function=data)

    def delete_function(self, name: str) -> None:
        self._sig = None
        self.daemon.call("functions.delete", lambda _r: self.refresh({}, set()), None, name=name)

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import functions", "", "Jeeves functions (*.json)")
        if not path:
            return
        try:
            with open(path) as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            show_message(self, "Import", f"Couldn't read it: {exc}")
            return
        self.daemon.call("functions.import", lambda _r: show_message(
            self, "Import", "Approve or deny the functions in the popup."), None, manifest=manifest)

    def export_file(self) -> None:
        def got(manifest: Any) -> None:
            if not manifest.get("functions"):
                show_message(self, "Export", "You haven't made any custom functions yet.")
                return
            path, _ = QFileDialog.getSaveFileName(self, "Export functions", "my-jeeves-functions.json",
                                                  "Jeeves functions (*.json)")
            if path:
                with open(path, "w") as fh:
                    json.dump(manifest, fh, indent=2)
        self.daemon.call("functions.export", got, None)
