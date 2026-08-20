"""Platform policy for libmpv hardware decode in the embedded render path."""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("mpv")

from desktop_client.mpv_view import _render_hwdec_mode


def test_linux_embedded_render_uses_copy_back_hwdec():
    assert _render_hwdec_mode(False, "linux") == "auto-copy-safe"


def test_other_platforms_retain_safe_auto_hwdec():
    assert _render_hwdec_mode(False, "win32") == "auto-safe"


def test_no_hwdec_always_forces_software_decode():
    assert _render_hwdec_mode(True, "linux") == "no"
    assert _render_hwdec_mode(True, "win32") == "no"
