"""
Shared QSS reskin for QComboBox -- theme colors (card background,
accent border/highlight, rounded corners) instead of native/KDE
chrome, applied everywhere a QComboBox is used across Settings and
the Add Filter dialog. A genuine custom dropdown (its own popup
widget, not a styled QComboBox) would be a bigger, separate rebuild;
this covers the "fully custom" LOOK for every combo box in the app
without that.
"""
from __future__ import annotations


def combo_box_stylesheet(appearance) -> str:
    """The theme's own combo box (ui_kit/skin.py); the arrow is painted by ThemedComboBox."""
    from .skin import combo_stylesheet
    from .theme import Theme
    return combo_stylesheet(Theme(appearance))
