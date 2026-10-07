"""The settings window's own icons, drawn here (in the theme's colors) rather than taken from the
icon theme: Main is the Jeeves bow tie, Settings a gear."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def gear(size: int, color: QColor, teeth: int = 8) -> QPixmap:
    pm, p = _canvas(size)
    c = size / 2
    outer, inner, hole = size * 0.46, size * 0.34, size * 0.14
    path = QPainterPath()
    step = 2 * math.pi / teeth
    for i in range(teeth):
        a = i * step
        pts = [(a - step * 0.22, outer), (a + step * 0.22, outer), (a + step * 0.32, inner), (a + step * 0.68, inner)]
        for j, (ang, r) in enumerate(pts):
            pt = QPointF(c + r * math.cos(ang), c + r * math.sin(ang))
            if i == 0 and j == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
    path.closeSubpath()
    ring = QPainterPath()
    ring.addEllipse(QPointF(c, c), hole, hole)
    path = path.subtracted(ring)
    p.fillPath(path, color)
    p.end()
    return pm


def bow_tie(size: int, color: QColor, knot: QColor) -> QPixmap:
    pm, p = _canvas(size)
    c, h = size / 2, size * 0.30
    left = QPainterPath()
    left.moveTo(c - size * 0.08, c)
    left.lineTo(size * 0.06, c - h)
    left.quadTo(size * 0.0, c, size * 0.06, c + h)
    left.closeSubpath()
    right = QPainterPath()
    right.moveTo(c + size * 0.08, c)
    right.lineTo(size * 0.94, c - h)
    right.quadTo(size * 1.0, c, size * 0.94, c + h)
    right.closeSubpath()
    p.fillPath(left, color)
    p.fillPath(right, color)
    p.setPen(Qt.NoPen)
    p.setBrush(knot)
    p.drawRoundedRect(QRectF(c - size * 0.11, c - size * 0.13, size * 0.22, size * 0.26), size * 0.05, size * 0.05)
    p.end()
    return pm


def main_icon(size: int = 64) -> QPixmap:
    """The Jeeves bow tie, as on the logo."""
    from ..resources import icon_path
    pm = QPixmap(str(icon_path(256)))
    if not pm.isNull():
        # trim the logo's transparent margin, so the tie fills the button's icon space like the gear
        img = pm.toImage()
        xs, ys = [], []
        for y in range(0, img.height(), 2):
            for x in range(0, img.width(), 2):
                c = img.pixelColor(x, y)
                # the tie's red cloth only: the sound waves either side would shrink it to a speck
                if c.alpha() > 40 and c.red() > 90 and c.red() > c.green() * 1.8:
                    xs.append(x)
                    ys.append(y)
        if xs:
            pad = 4
            pm = pm.copy(min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad + 2, max(ys) - min(ys) + 2 * pad + 2)
        return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return bow_tie(size, QColor("#c8102e"), QColor("#e8b923"))
