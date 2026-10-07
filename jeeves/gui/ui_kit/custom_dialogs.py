"""
The kit's own dialogs for the things Qt would otherwise open a native
window for: asking for a line of text, picking a color, and picking a file
or folder. Frameless, rounded, theme-colored -- the CustomMessageDialog
treatment -- and sized to their contents (never cut off).
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QListWidgetItem, QVBoxLayout, QWidget

from .custom_button import CustomButton
from .custom_line_edit import CustomLineEdit
from .custom_text import CustomListWidget
from .rounded_rect import rounded_rect_path
from .theme import Theme
from .theme_config import get_settings


def fit_to_width(widget: QWidget, width: int) -> None:
    """Size a window to its contents at this width -- word-wrapped labels included, which a plain
    resize() or adjustSize() cut off."""
    lay = widget.layout()
    if lay is None:
        widget.resize(width, widget.sizeHint().height())
        return
    lay.activate()
    h = lay.heightForWidth(width) if lay.hasHeightForWidth() else -1
    h = max(h, lay.sizeHint().height(), lay.minimumSize().height())
    widget.setMinimumWidth(min(width, lay.minimumSize().width() or width))
    widget.resize(width, h)


class KitDialog(QDialog):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._appearance = get_settings()
        self._theme = Theme(self._appearance)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(24, 20, 24, 20)
        self.lay.setSpacing(12)
        heading = QLabel(title)
        heading.setWordWrap(True)
        heading.setStyleSheet(f"color: {self._appearance.color_text}; font-size: 15px; font-weight: bold;")
        self.lay.addWidget(heading)

    def label(self, text: str) -> QLabel:
        lab = QLabel(text)
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color: {self._appearance.color_text};")
        self.lay.addWidget(lab)
        return lab

    def buttons(self, ok: str = "OK", cancel: str = "Cancel") -> CustomButton:
        row = QHBoxLayout()
        row.addStretch(1)
        if cancel:
            c = CustomButton(cancel)
            c.clicked.connect(self.reject)
            row.addWidget(c)
        o = CustomButton(ok)
        o.clicked.connect(self.accept)
        row.addWidget(o)
        self.lay.addLayout(row)
        return o

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        radius = self._appearance.rounded_corner_radius if self._appearance.rounded_corners_enabled else 16
        if radius:
            p.fillPath(rounded_rect_path(rect, radius), self._theme.page_background())
        else:
            p.fillRect(rect, self._theme.page_background())
        p.end()


def ask_text(parent, title: str, prompt: str, text: str = "", placeholder: str = "") -> tuple[str, bool]:
    """(text, ok) -- the kit's QInputDialog.getText."""
    d = KitDialog(title, parent)
    d.label(prompt)
    edit = CustomLineEdit(text)
    edit.setPlaceholderText(placeholder)
    d.lay.addWidget(edit)
    d.buttons()
    edit.returnPressed.connect(d.accept)
    fit_to_width(d, 420)
    edit.setFocus()
    ok = d.exec() == QDialog.Accepted
    return edit.text(), ok


SWATCHES = ["#000000", "#ffffff", "#808080", "#c0c0c0", "#ff2020", "#ff8a00", "#ffcc00", "#20b070", "#00b8d4",
            "#2f6fff", "#8a2be2", "#ff40a0", "#7a4a2a", "#1d1d1d", "#2c3e50", "#16a085"]


class _Swatch(QWidget):
    def __init__(self, color: str, pick) -> None:
        super().__init__()
        self.color = color
        self.pick = pick
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(color)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(128, 128, 128))
        p.setBrush(QColor(self.color))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        p.end()

    def mousePressEvent(self, _e) -> None:
        self.pick(self.color)


def pick_color(parent, title: str, current: str = "#808080") -> str | None:
    """A color: a swatch or any #rrggbb -- the kit's QColorDialog.getColor."""
    d = KitDialog(title, parent)
    preview = QLabel()
    preview.setFixedHeight(28)
    edit = CustomLineEdit(QColor(current).name() if QColor(current).isValid() else "#808080")

    def show(c: str) -> None:
        if QColor(c).isValid():
            preview.setStyleSheet(f"background: {QColor(c).name()}; border-radius: 8px;")

    def picked(c: str) -> None:
        edit.setText(c)
        show(c)
    grid = QGridLayout()
    for i, c in enumerate(SWATCHES):
        grid.addWidget(_Swatch(c, picked), i // 8, i % 8)
    d.lay.addLayout(grid)
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    row.addWidget(preview, 1)
    d.lay.addLayout(row)
    edit.textChanged.connect(show)
    show(edit.text())
    d.buttons()
    fit_to_width(d, 340)
    if d.exec() != QDialog.Accepted or not QColor(edit.text()).isValid():
        return None
    return QColor(edit.text()).name()


def pick_path(parent, title: str, start: str = "", mode: str = "open", suffix: str = "",
              default_name: str = "") -> str:
    """A file or folder (mode: open | save | folder) -- the kit's QFileDialog. "" when cancelled."""
    d = KitDialog(title, parent)
    folder = [Path(os.path.expanduser(start or "~"))]
    if folder[0].is_file():
        folder[0] = folder[0].parent
    where = CustomLineEdit(str(folder[0]))
    d.lay.addWidget(where)
    listing = CustomListWidget()
    listing.setMinimumHeight(300)
    d.lay.addWidget(listing, 1)
    name = CustomLineEdit(default_name)
    name.setPlaceholderText("File name")
    if mode == "save":
        d.lay.addWidget(name)

    def fill() -> None:
        listing.clear()
        here = folder[0]
        where.setText(str(here))
        if here.parent != here:
            QListWidgetItem("⬑  ..", listing).setData(Qt.UserRole, str(here.parent))
        try:
            entries = sorted(here.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            entries = []
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                QListWidgetItem(f"📁  {p.name}", listing).setData(Qt.UserRole, str(p))
            elif mode != "folder" and (not suffix or p.name.endswith(suffix)):
                QListWidgetItem(f"     {p.name}", listing).setData(Qt.UserRole, str(p))

    def go(item) -> None:
        p = Path(item.data(Qt.UserRole))
        if p.is_dir():
            folder[0] = p
            fill()
        elif mode == "open":
            chosen[0] = str(p)
            d.accept()
        else:
            name.setText(p.name)

    def typed() -> None:
        p = Path(os.path.expanduser(where.text()))
        if p.is_dir():
            folder[0] = p
            fill()
    chosen = [""]
    listing.itemActivated.connect(go)
    listing.itemClicked.connect(lambda it: name.setText(Path(it.data(Qt.UserRole)).name)
                                if mode == "save" and Path(it.data(Qt.UserRole)).is_file() else None)
    where.returnPressed.connect(typed)
    ok = d.buttons("Choose folder" if mode == "folder" else "Save" if mode == "save" else "Open")
    fill()
    d.resize(560, 520)
    if d.exec() != QDialog.Accepted:
        return ""
    if chosen[0]:
        return chosen[0]
    if mode == "folder":
        return str(folder[0])
    if mode == "save":
        n = name.text().strip()
        if not n:
            return ""
        if suffix and not n.endswith(suffix):
            n += suffix
        return str(folder[0] / n)
    cur = listing.currentItem()
    p = Path(cur.data(Qt.UserRole)) if cur is not None else None
    del ok
    return str(p) if p is not None and p.is_file() else ""
