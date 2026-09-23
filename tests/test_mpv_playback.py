"""Desktop player state across keyboard actions and stream transitions."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from desktop_client.mpv_view import MpvPlayerView
from desktop_client.options import DesktopOptions


def test_keyboard_pause_intent_survives_epoch_release():
    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-playback",
    ))
    try:
        player.pause_requested.connect(lambda: player.set_paused(True))
        player.keyPressEvent(QKeyEvent(
            QKeyEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " ",
        ))
        player._restart_seen = True
        player._external_ready = True
        player._prebuffer_ready = True
        player._maybe_release_epoch()
        assert player._caller_paused
        assert player.mpv.pause
    finally:
        player.stop()
        player.mpv.terminate()
        player.close()
