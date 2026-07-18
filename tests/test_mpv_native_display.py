import pytest

pytest.importorskip("PySide6")  # desktop client is an optional extra

"""Native-display propagation for libmpv hardware-decoder interop."""

from ctypes import c_void_p

from desktop_client.mpv_view import _native_display_params


class _FakeNativeInterface:
    def __init__(self, display):
        self._display = display

    def display(self):
        return self._display


class _FakeApplication:
    def __init__(self, platform: str, display=0x1234):
        self._platform = platform
        self._native = _FakeNativeInterface(display)

    def platformName(self):
        return self._platform

    def nativeInterface(self):
        return self._native


def test_wayland_display_is_passed_to_libmpv():
    params = _native_display_params(_FakeApplication("wayland"))

    assert set(params) == {"wl_display"}
    assert isinstance(params["wl_display"], c_void_p)
    assert params["wl_display"].value == 0x1234


def test_x11_display_is_passed_to_libmpv():
    params = _native_display_params(_FakeApplication("xcb"))

    assert set(params) == {"x11_display"}
    assert isinstance(params["x11_display"], c_void_p)
    assert params["x11_display"].value == 0x1234


def test_non_native_and_missing_displays_add_no_render_parameter():
    assert _native_display_params(_FakeApplication("offscreen")) == {}
    assert _native_display_params(_FakeApplication("wayland", display=0)) == {}
