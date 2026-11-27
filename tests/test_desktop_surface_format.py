"""The player's windows ask for 8 bits per colour channel."""

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QSurfaceFormat

from desktop_client.app import request_true_color


@pytest.fixture
def default_format():
    saved = QSurfaceFormat.defaultFormat()
    yield
    QSurfaceFormat.setDefaultFormat(saved)


# Bugs caught: 0, False positives: 0
def test_default_surface_format_asks_for_8_bits_per_channel(default_format):
    # Asking for nothing let NVIDIA's EGL on Wayland pick RGB565 for the
    # window: every frame mpv rendered was cut to 5/6/5 bits on its way to
    # the screen, banding gradients in coloured steps (2026-11-26).
    QSurfaceFormat.setDefaultFormat(QSurfaceFormat())
    request_true_color()
    fmt = QSurfaceFormat.defaultFormat()
    assert (fmt.redBufferSize(), fmt.greenBufferSize(), fmt.blueBufferSize()) == (8, 8, 8)


# Bugs caught: 0, False positives: 0
def test_deeper_requests_are_kept(default_format):
    fmt = QSurfaceFormat()
    fmt.setRedBufferSize(10); fmt.setGreenBufferSize(10); fmt.setBlueBufferSize(10)
    fmt.setAlphaBufferSize(2)
    QSurfaceFormat.setDefaultFormat(fmt)
    request_true_color()
    fmt = QSurfaceFormat.defaultFormat()
    assert (fmt.redBufferSize(), fmt.greenBufferSize(), fmt.blueBufferSize(), fmt.alphaBufferSize()) == (10, 10, 10, 2)
