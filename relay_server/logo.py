"""The tray-GUI logo, drawn at runtime (no bundled asset file).

Kept separate from ``tray`` so ``packaging/make_icon.py`` can render it
without importing the server stack. Also the source of truth for the frozen
exe's embedded icon: that script writes ``packaging/icon.ico`` from this
drawing, so regenerate the .ico after changing it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap


def paint_logo(size: int) -> QPixmap:
    """The app logo drawn at the requested pixel size."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2d7d9a"))
    margin = max(1, round(size * 4 / 64))
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(6, round(size * 0.62)))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(pix.rect(), Qt.AlignCenter, "U")
    painter.end()
    return pix


def make_icon() -> QIcon:
    """A multi-size QIcon of the logo (crisp from tray size up to 256px)."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(paint_logo(size))
    return icon
