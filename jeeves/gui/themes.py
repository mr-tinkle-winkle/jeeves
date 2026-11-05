"""The window's looks. Each theme is a full set of ThemeSettings -- colors, fonts, shapes, the page
pattern, the tab style and how things move -- written into settings.theme when it's picked (the
Appearance editor can still adjust it afterwards).

Fonts are bundled (resources/fonts, SIL Open Font License), so every theme looks the same on any
desktop."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .ui_kit import ThemeSettings

FONT_DIR = Path(__file__).resolve().parent.parent / "resources" / "fonts"


@dataclass(frozen=True)
class Look:
    key: str
    name: str
    blurb: str
    values: dict[str, Any] = field(default_factory=dict)

    def settings(self) -> ThemeSettings:
        return ThemeSettings(**{**asdict(ThemeSettings()), **self.values, "preset": self.key})


LOOKS: dict[str, Look] = {}


def _look(key: str, name: str, blurb: str, **values: Any) -> None:
    LOOKS[key] = Look(key, name, blurb, values)


_look("classic", "Classic", "The original grey look.")

_look("ledger", "The butler's ledger",
      "Warm near-black, brass and a serif: Jeeves as a gentleman's gentleman. Things glide in.",
      color_accent="#5a4e3c", color_surface="#241f19", color_app_background="#1d1914",
      color_page_background="#16130f", color_highlight="#3a3226", color_text="#ece4d6",
      color_text_outline="#0d0b08", color_pop="#d9b56c", color_muted="#b3a68f",
      rounded_corners_enabled=True, rounded_corner_radius=4, ui_padding=16, border_width=1.0,
      font_body="Manrope", font_display="Cormorant Garamond", font_mono="JetBrains Mono", font_size=10,
      pattern="", tab_style="underline", motion="glide", vocab="ledger")

_look("alive", "Alive",
      "Soft and rounded; every agent wears its own color, and a live one pulses. Things spring in.",
      color_accent="#3a3344", color_surface="#221e26", color_app_background="#0d0d10",
      color_page_background="#17141a", color_highlight="#2c2732", color_text="#f1f1f4",
      color_text_outline="#000000", color_pop="#ff6b57", color_muted="#a6a6b3",
      rounded_corners_enabled=True, rounded_corner_radius=22, ui_padding=16, border_width=1.0,
      font_body="Sora", font_display="Sora", font_mono="JetBrains Mono", font_size=10,
      pattern="", tab_style="pill", motion="spring")

_look("console", "Console",
      "A terminal: monospace, square, black with amber. Text types itself out; a cursor blinks.",
      color_accent="#2a2a2a", color_surface="#0c0c0c", color_app_background="#050505",
      color_page_background="#050505", color_highlight="#1a1a1a", color_text="#d8d8d8",
      color_text_outline="#000000", color_pop="#ffb000", color_muted="#9a9a9a",
      rounded_corners_enabled=True, rounded_corner_radius=0, ui_padding=14, border_width=1.0,
      font_body="JetBrains Mono", font_display="Space Grotesk", font_mono="JetBrains Mono", font_size=10,
      pattern="", tab_style="brackets", motion="type")

_look("brutalist", "Brutalist",
      "Black on white, thick borders, huge capitals and one yellow. Things slam into place.",
      color_accent="#000000", color_surface="#ffffff", color_app_background="#ffffff",
      color_page_background="#ffffff", color_highlight="#e8e8e8", color_text="#000000",
      color_text_outline="#ffffff", color_pop="#ffe600", color_muted="#3d3d3d",
      color_shadow="#000000", shadow_offset=0,
      rounded_corners_enabled=True, rounded_corner_radius=0, ui_padding=14, border_width=4.0,
      font_body="Archivo", font_display="Archivo Black", font_mono="JetBrains Mono", font_size=10,
      display_upper=True, pattern="", tab_style="block", motion="slam")

_look("arcade", "Arcade",
      "Pixel fonts, scanlines, pink and cyan with offset shadows. Things blink and step.",
      color_accent="#ff4fd8", color_surface="#2e1a52", color_app_background="#140a24",
      color_page_background="#1e1036", color_highlight="#3b2466", color_text="#f4ecff",
      color_text_outline="#140a24", color_pop="#ffe14d", color_muted="#c9b6e8",
      color_shadow="#4df3ff", shadow_offset=5,
      rounded_corners_enabled=True, rounded_corner_radius=0, ui_padding=16, border_width=3.0,
      font_body="VT323", font_display="Press Start 2P", font_mono="VT323", font_size=15,
      display_upper=True, pattern="scanlines", tab_style="block", motion="step", vocab="arcade")

_look("blueprint", "Blueprint",
      "White line work on a blue grid, like a technical drawing. Things draw themselves in.",
      color_accent="#ffffff", color_surface="#163f73", color_app_background="#0f3260",
      color_page_background="#123a6b", color_highlight="#1d4a82", color_text="#ffffff",
      color_text_outline="#0b2649", color_pop="#ffffff", color_muted="#c4d6ee",
      rounded_corners_enabled=True, rounded_corner_radius=0, ui_padding=16, border_width=1.5,
      font_body="IBM Plex Mono", font_display="IBM Plex Sans Condensed", font_mono="IBM Plex Mono",
      font_size=10, display_upper=True, pattern="grid", tab_style="brackets", motion="draw")


# What things are called, per vocabulary (the ledger's butler, the arcade's game).
VOCAB: dict[str, dict[str, str]] = {
    "": {"overview": "Overview", "personality": "Personality", "voice": "Voice", "brain": "Brain",
         "abilities": "Abilities", "sources": "Sources", "history": "History", "talk": "Talk",
         "new_agent": "+ New agent", "activity": "Activity", "models": "Models", "settings": "Settings",
         "agents": "Agents", "shared": "Shared", "idle": "Idle", "listening": "Listening",
         "thinking": "Thinking", "speaking": "Speaking", "working": "Working", "off": "Off", "on_switch": "On",
         "recent": "Recently"},
    "ledger": {"personality": "Character", "brain": "Mind", "abilities": "Duties", "sources": "Library",
               "history": "Ledger", "talk": "Ring for {name}", "new_agent": "+ Engage a new agent",
               "activity": "The ledger", "models": "Model library", "settings": "House rules",
               "agents": "The household", "shared": "Below stairs", "idle": "Resting",
               "listening": "At your service", "thinking": "Considering", "speaking": "Speaking",
               "recent": "From the ledger", "off": "Off duty", "on_switch": "On duty"},
    "arcade": {"overview": "Stats", "personality": "Persona", "abilities": "Skills", "history": "Log",
               "talk": "Press start", "new_agent": "+ Insert coin", "settings": "Options",
               "agents": "Select agent", "idle": "Idle", "listening": "Ready", "thinking": "Loading",
               "speaking": "Speaking", "recent": "Quest log", "off": "Paused", "on_switch": "Player on"},
}


def words(vocab: str) -> dict[str, str]:
    return {**VOCAB[""], **VOCAB.get(vocab, {})}


def load_fonts() -> None:
    """Every bundled face, once per process (before any theme font is asked for)."""
    from PySide6.QtGui import QFontDatabase
    global _loaded
    if _loaded:
        return
    _loaded = True
    for f in sorted(FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(f))


_loaded = False

# Agent colors for themes that give each agent its own (Alive): picked by the agent's place in the list.
AGENT_COLORS = ["#ff6b57", "#4fc9da", "#b78cff", "#f5c04a", "#6ddc8f", "#ff8fc7"]
