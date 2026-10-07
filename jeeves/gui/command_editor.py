"""Agents > Commands: one card per command the agent may run. The command line's {placeholders}
become its arguments, and each gets a row to explain what it is -- that explanation is what the
agent reads to fill it in."""
from __future__ import annotations

import re
from typing import Any, Callable

from PySide6.QtWidgets import QGridLayout, QVBoxLayout, QWidget

from .ui_kit import CustomButton, CustomCheckBox, CustomGroupBox, CustomLineEdit, CustomSpinBox
from .widgets import discard, label, row

PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w]*)\}")

EXAMPLES = [
    {"name": "rebuild", "description": "Rebuilds and switches to the NixOS configuration",
     "command": "nixos-rebuild switch --flake ~/nix#{host}",
     "args": [{"name": "host", "description": "which machine's configuration to build", "choices": [],
               "required": False}], "confirm": True, "terminal": True, "timeout": 1800},
    {"name": "push-nix-config", "description": "Commits and pushes the NixOS configuration repository",
     "command": "nix-push {message}",
     "args": [{"name": "message", "description": "the commit message, short, describing what changed",
               "choices": [], "required": True}], "confirm": True, "terminal": False, "timeout": 300},
]


class ArgRow(QWidget):
    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__()
        self.name = name
        lay = QGridLayout(self)
        lay.setContentsMargins(16, 0, 0, 0)
        title = label(f"{{{name}}}", False)
        title.setMinimumWidth(90)
        lay.addWidget(title, 0, 0)
        self.desc = CustomLineEdit()
        self.desc.setPlaceholderText(f"What {name} is, so the agent knows what to put there")
        self.desc.setText(spec.get("description") or "")
        lay.addWidget(self.desc, 0, 1)
        self.required = CustomCheckBox("Required")
        self.required.setToolTip("Unticked: the agent may leave it out (the whole argument is dropped).")
        self.required.setChecked(bool(spec.get("required", False)))
        lay.addWidget(self.required, 0, 2)
        self.choices = CustomLineEdit()
        self.choices.setPlaceholderText("Allowed values, comma separated (empty = anything)")
        self.choices.setText(", ".join(str(c) for c in spec.get("choices") or []))
        lay.addWidget(self.choices, 1, 1, 1, 2)
        lay.setColumnStretch(1, 1)

    def value(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.desc.text().strip(),
                "choices": [c.strip() for c in self.choices.text().split(",") if c.strip()],
                "required": self.required.isChecked()}


class CommandCard(CustomGroupBox):
    def __init__(self, spec: dict[str, Any], on_remove: Callable[["CommandCard"], None]) -> None:
        super().__init__(spec.get("name") or "New command")
        self._extra = {k: v for k, v in spec.items()
                       if k not in ("name", "description", "command", "args", "confirm", "terminal", "timeout")}
        lay = self.make_layout(QVBoxLayout)
        self.name = CustomLineEdit()
        self.name.setPlaceholderText("rebuild")
        self.name.setText(spec.get("name") or "")
        self.name.textChanged.connect(lambda t: self.setTitle(t.strip() or "New command"))
        lay.addWidget(row(label("Name", False), self.name, stretch_last=True))
        self.desc = CustomLineEdit()
        self.desc.setPlaceholderText("What it does, e.g. Rebuilds and switches to the NixOS configuration")
        self.desc.setText(spec.get("description") or "")
        lay.addWidget(row(label("What it does", False), self.desc, stretch_last=True))
        self.command = CustomLineEdit()
        self.command.setPlaceholderText("nixos-rebuild switch --flake ~/nix#{host}")
        self.command.setText(spec.get("command") or "")
        lay.addWidget(row(label("Command line", False), self.command, stretch_last=True))
        lay.addWidget(label("Write {something} where an argument goes. Each one gets a line below: describe "
                            "it, and the agent fills it in from what you ask (\"rebuild the laptop\" → "
                            "host = laptop). Run without a shell, so pipes and && don't work -- point it at "
                            "a script for that."))
        self.args_box = QWidget()
        self.args_layout = QVBoxLayout(self.args_box)
        self.args_layout.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.args_box)
        self.no_args = label("No arguments: it always runs exactly as written.")
        lay.addWidget(self.no_args)
        self._known = {a.get("name"): a for a in spec.get("args") or [] if isinstance(a, dict) and a.get("name")}
        self.rows: dict[str, ArgRow] = {}
        self.command.textChanged.connect(lambda _t: self._sync_args())
        self.confirm = CustomCheckBox("Ask before running")
        self.confirm.setChecked(bool(spec.get("confirm", True)))
        self.terminal = CustomCheckBox("Run in a terminal window (to watch it, or type a sudo password)")
        self.terminal.setChecked(bool(spec.get("terminal", False)))
        lay.addWidget(self.confirm)
        lay.addWidget(self.terminal)
        self.timeout = CustomSpinBox()
        self.timeout.setRange(5, 86400)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(int(spec.get("timeout") or 600))
        self.timeout.wheelEvent = lambda e: e.ignore()
        self.terminal.toggled.connect(lambda on: self.timeout.setEnabled(not on))
        self.timeout.setEnabled(not self.terminal.isChecked())
        remove = CustomButton("Remove")
        remove.clicked.connect(lambda: on_remove(self))
        lay.addWidget(row(label("Stop it after", False), self.timeout, remove))
        self._sync_args()

    def _sync_args(self) -> None:
        names = list(dict.fromkeys(PLACEHOLDER.findall(self.command.text())))
        for n, r in list(self.rows.items()):
            if n not in names:
                self._known[n] = r.value()
                discard(r)
                del self.rows[n]
        for n in names:
            if n not in self.rows:
                self.rows[n] = ArgRow(n, self._known.get(n, {}))
        for n in names:                      # keep the command line's order
            self.args_layout.removeWidget(self.rows[n])
            self.args_layout.addWidget(self.rows[n])
        self.no_args.setVisible(not names)

    def value(self) -> dict[str, Any]:
        names = list(dict.fromkeys(PLACEHOLDER.findall(self.command.text())))
        return {**self._extra, "name": self.name.text().strip(), "description": self.desc.text().strip(),
                "command": self.command.text().strip(), "args": [self.rows[n].value() for n in names],
                "confirm": self.confirm.isChecked(), "terminal": self.terminal.isChecked(),
                "timeout": self.timeout.value()}


class CommandList(QWidget):
    """All of one agent's commands, with + Add command and the two examples to start from."""

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.cards_box = QWidget()
        self.cards = QVBoxLayout(self.cards_box)
        self.cards.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.cards_box)
        self.empty = label("No commands yet.")
        lay.addWidget(self.empty)
        add = CustomButton("+ Add command")
        add.clicked.connect(lambda: self.add({}))
        example = CustomButton("+ Example: rebuild")
        example.clicked.connect(lambda: self.add(EXAMPLES[0]))
        example2 = CustomButton("+ Example: push-nix-config")
        example2.clicked.connect(lambda: self.add(EXAMPLES[1]))
        lay.addWidget(row(add, example, example2))
        self._cards: list[CommandCard] = []

    def add(self, spec: dict[str, Any]) -> CommandCard:
        import copy
        card = CommandCard(copy.deepcopy(spec), self._remove)
        self._cards.append(card)
        self.cards.addWidget(card)
        self.empty.setVisible(False)
        return card

    def _remove(self, card: CommandCard) -> None:
        if card in self._cards:
            self._cards.remove(card)
            discard(card)
        self.empty.setVisible(not self._cards)

    def load(self, specs: list[dict[str, Any]]) -> None:
        for c in self._cards:
            discard(c)
        self._cards = []
        for s in specs or []:
            if isinstance(s, dict):
                self.add(s)
        self.empty.setVisible(not self._cards)

    def value(self) -> list[dict[str, Any]]:
        return [c.value() for c in self._cards if c.name.text().strip() and c.command.text().strip()]
