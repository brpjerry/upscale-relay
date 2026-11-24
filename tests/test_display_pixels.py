"""The display size a session negotiates, in physical pixels."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import QSize

from desktop_client import hdr_output
from desktop_client.main_window import MainWindow


def _window(logical: QSize, window_dpr: float, screen_dpr: float, name: str = "DP-3"):
    screen = SimpleNamespace(
        size=lambda: logical, devicePixelRatio=lambda: screen_dpr, name=lambda: name,
    )
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
def test_display_pixels_use_the_window_scale(monkeypatch, logical, window_dpr, screen_dpr, expected):
    # No output mode to read (off Wayland): the window's scale decides.
    monkeypatch.setattr(hdr_output, "wayland_output_pixels", lambda _screen: None)
    window = _window(logical, window_dpr, screen_dpr)
    assert MainWindow._display_pixels(window) == expected


# Bugs caught: 0, False positives: 0
def test_display_pixels_prefer_the_wayland_output_mode(monkeypatch):
    # A window across a 1080p output at 100% and one at 200% can be drawn at
    # 2 while Qt places it on the 1080p one: the mode, not the scale, decides.
    monkeypatch.setattr(hdr_output, "wayland_output_pixels", lambda screen: (1920, 1080))
    window = _window(QSize(1920, 1080), 2.0, 1.0, name="HDMI-A-1")
    assert MainWindow._display_pixels(window) == (1920, 1080)


def _client(outputs):
    return SimpleNamespace(outputs=outputs, roundtrip=lambda: None)


# Bugs caught: 0, False positives: 0
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"name": "DP-3", "mode": (3840, 2160), "swap_axes": False}, (3840, 2160)),
        # Rotated 90 or 270 degrees: the mode is reported unrotated.
        ({"name": "DP-3", "mode": (3840, 2160), "swap_axes": True}, (2160, 3840)),
        # No current mode yet, or a nameless (pre-v4) output: no answer.
        ({"name": "DP-3"}, None),
        ({"name": None, "mode": (3840, 2160)}, None),
        ({"name": "HDMI-A-1", "mode": (3840, 2160)}, None),
    ],
)
def test_wayland_output_pixels_match_the_screen_by_name(monkeypatch, output, expected):
    monkeypatch.setattr(hdr_output, "platform_kind", lambda app=None: "wayland")
    monkeypatch.setattr(hdr_output, "_wayland_client", lambda display=None: _client({7: output}))
    screen = SimpleNamespace(name=lambda: "DP-3")
    assert hdr_output.wayland_output_pixels(screen) == expected


# Bugs caught: 0, False positives: 0
def test_wayland_output_pixels_fall_back_off_wayland_and_on_errors(monkeypatch):
    screen = SimpleNamespace(name=lambda: "DP-3")
    monkeypatch.setattr(hdr_output, "platform_kind", lambda app=None: "xcb")
    assert hdr_output.wayland_output_pixels(screen) is None

    def broken(display=None):
        raise OSError("wayland roundtrip failed")

    monkeypatch.setattr(hdr_output, "platform_kind", lambda app=None: "wayland")
    monkeypatch.setattr(hdr_output, "_wayland_client", broken)
    assert hdr_output.wayland_output_pixels(screen) is None
