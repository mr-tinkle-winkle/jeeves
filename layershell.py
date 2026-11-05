"""Layer-shell for the overlay's indicator windows (Wayland). Same approach as
afterglow's clip indicator, which works on KDE Plasma.

A normal Qt window can't place itself on Wayland and won't stay above other
windows. The indicators have to be *layer-shell surfaces* on the ``overlay``
layer: no keyboard interactivity, exclusive zone 0, anchored to a screen edge
or corner. KDE's implementation is LayerShellQt, which has a C++ API only, so
``native/jeeves_layershell.cpp`` is a small shim (built in the flake) exposing::

    int jeeves_layershell_configure(void *qwindow, int anchors,
                                    int top, int right, int bottom, int left)

Rules:
  * the shim and PySide6 must use the SAME Qt -- the flake builds both from one
    nixpkgs;
  * ``configure`` runs after the QWindow exists (``widget.winId()``) and before
    it is shown;
  * ``QT_WAYLAND_SHELL_INTEGRATION=layer-shell`` turns EVERY window of a process
    into a layer surface, so it is only set in the indicator process
    (``jeeves overlay``), never in the popups process or the GUI.

The library comes from ``JEEVES_LAYERSHELL_LIB`` (set by the Nix wrapper) or
``native/build`` in a checkout. Without it ``available()`` is False and the
overlay falls back to an XWayland window.
"""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

log = logging.getLogger("jeeves.layershell")

ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 1, 2, 4, 8    # LayerShellQt::Window::Anchor
_BITS = {"top": ANCHOR_TOP, "bottom": ANCHOR_BOTTOM, "left": ANCHOR_LEFT, "right": ANCHOR_RIGHT}
LIB_NAME = "libjeeves_layershell.so"

_lib = None
_tried = False


def anchor_bits(edges) -> int:
    n = 0
    for edge in edges:
        n |= _BITS[edge]
    return n


def corner_edges(corner: str) -> list[str]:
    """'top-right' -> ['top', 'right']."""
    return [part for part in corner.split("-") if part in _BITS]


def _candidates() -> list[Path]:
    out = []
    env = os.environ.get("JEEVES_LAYERSHELL_LIB")
    if env:
        out.append(Path(env))
    root = Path(__file__).resolve().parent.parent
    out += [root / "native" / "build" / LIB_NAME, root / "native" / LIB_NAME]
    return out


def _load():
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in _candidates():
        if path.is_file():
            try:
                lib = ctypes.CDLL(str(path))
                lib.jeeves_layershell_configure.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                                            ctypes.c_int, ctypes.c_int, ctypes.c_int]
                lib.jeeves_layershell_configure.restype = ctypes.c_int
                _lib = lib
                return _lib
            except (OSError, AttributeError) as exc:
                log.warning("layer-shell shim %s could not be loaded: %s", path, exc)
    return None


def available() -> bool:
    return _load() is not None


def enable_in_this_process() -> bool:
    """Indicator process only, before the QApplication is created."""
    if not available():
        return False
    os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
    return True


def configure(window, edges, margins=(0, 0, 0, 0)) -> bool:
    """Make a created, not-yet-shown QWindow an overlay-layer surface anchored to
    ``edges``. margins = (top, right, bottom, left)."""
    lib = _load()
    if lib is None or window is None:
        return False
    try:
        import shiboken6
        ptr = shiboken6.getCppPointer(window)[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("layer-shell: no native pointer for the window: %s", exc)
        return False
    t, r, b, l = (int(m) for m in margins)
    rc = lib.jeeves_layershell_configure(ctypes.c_void_p(ptr), anchor_bits(edges), t, r, b, l)
    if rc != 0:
        log.warning("layer-shell configure failed (rc=%s)", rc)
    return rc == 0
