"""Regenerate ``packaging/icon.ico`` from the tray logo drawing.

The .ico is checked in and embedded into the frozen exes by PyInstaller's
``--icon`` flag (see .github/workflows/release.yml), so the release workflow
never has to render it. Run this manually after changing ``paint_logo`` in
``relay_server.logo``:

    python packaging/make_icon.py

The container is assembled by hand because Qt's ICO writer stores a single
image; Windows expects the full size ladder (PNG-compressed entries are valid
from Vista on).
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Run on the native platform: offscreen has no font database here, which
    # turns the drawn glyph into a tofu box.
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication(sys.argv)
    from relay_server.logo import paint_logo

    pngs = []
    for size in _SIZES:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        paint_logo(size).save(buf, "PNG")
        pngs.append((size, bytes(buf.data())))

    header = bytearray(struct.pack("<HHH", 0, 1, len(pngs)))
    offset = 6 + 16 * len(pngs)
    images = bytearray()
    for size, png in pngs:
        header += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset
        )
        images += png
        offset += len(png)

    path = Path(__file__).with_name("icon.ico")
    path.write_bytes(bytes(header + images))
    print(f"wrote {path} ({path.stat().st_size} bytes, sizes {_SIZES})")


if __name__ == "__main__":
    main()
