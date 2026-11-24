"""The display size a session negotiates, in physical pixels."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import QSize

from desktop_client.main_window import MainWindow


def _window(logical: QSize, window_dpr: float, screen_dpr: float):
    screen = SimpleNamespace(size=lambda: logical, devicePixelRatio=lambda: screen_dpr)
    return SimpleNamespace(screen=lambda: screen, devicePixelRatioF=lambda: window_dpr)


# Bugs caught: 0, False positives: 0
@pytest.mark.parametrize(
    ("logical", "window_dpr", "screen_dpr", "expected"),
    [
        # Wayland, 4K at 150%: the screen reports the output's integer scale.
        (QSize(2560, 1440), 1.5, 2.0, (3840, 2160)),
        # The same panel rotated to portrait.
        (QSize(1440, 2560), 1.5, 2.0, (2160, 3840)),
        # Windows at 150%: window and screen agree.
        (QSize(2560, 1440), 1.5, 1.5, (3840, 2160)),
        (QSize(1920, 1080), 1.0, 1.0, (1920, 1080)),
    ],
)
def test_display_pixels_use_the_window_scale(logical, window_dpr, screen_dpr, expected):
    window = _window(logical, window_dpr, screen_dpr)
    assert MainWindow._display_pixels(window) == expected
