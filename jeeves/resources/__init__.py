"""Jeeves' own assets: the logo as square icons (icons/jeeves-<size>.png).

Sizes 48 px and up are the whole logo (the bow tie with its sound waves); 32 px and
below are the bow tie alone, since the waves turn to noise that small."""
from __future__ import annotations

from pathlib import Path
from typing import Any

ICON_DIR = Path(__file__).resolve().parent / "icons"
ICON_SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)


def icon_path(size: int = 256) -> Path:
    """The icon file closest to (and not smaller than, when possible) this size."""
    best = min(ICON_SIZES, key=lambda s: (s < size, abs(s - size)))
    return ICON_DIR / f"jeeves-{best}.png"


def app_icon() -> Any:
    """A QIcon carrying every size, so each place picks the right one."""
    from PySide6.QtGui import QIcon
    icon = QIcon()
    for s in ICON_SIZES:
        p = ICON_DIR / f"jeeves-{s}.png"
        if p.is_file():
            icon.addFile(str(p))
    return icon
