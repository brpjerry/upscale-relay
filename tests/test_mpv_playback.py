"""Desktop player state across keyboard actions and stream transitions."""

import os
import asyncio

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


def test_local_playback_reports_tracks_position_and_accepts_transport(tmp_path):
    from upscale_cli.sample import make_sample

    path = tmp_path / "original.mkv"
    make_sample(str(path), frames=144, width=64, height=64, fps=24)
    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-local-playback",
    ))
    positions = []
    track_reports = []
    output_reports = []
    player.position_changed.connect(positions.append)
    player.audio_track_list_changed.connect(lambda tracks, selected: track_reports.append(tracks))
    player.volume_changed.connect(lambda volume, muted: output_reports.append((volume, muted)))

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.05)

    async def scenario():
        try:
            await player.play_local(str(path), 1.0, paused=True)
            await wait_until(lambda: positions and track_reports)
            assert player.mpv.pause
            assert 0.9 <= positions[-1] <= 1.1
            # mpv key bindings/config can change output independently of Qt.
            player.mpv.volume = 37
            player.mpv.mute = True
            await wait_until(lambda: output_reports[-1:] == [(37, True)])
            player.set_volume(65)
            player.set_muted(False)
            assert player.audio_output_state() == (65, False)
            player.seek_local(2.0)
            await wait_until(lambda: positions[-1] >= 1.9)
            assert player.mpv.pause  # seeking preserves caller pause intent
            player.set_paused(False)
            await wait_until(lambda: positions[-1] > 2.2)
            assert player._stats_task is not None and not player._stats_task.done()
        finally:
            player.stop()
            await asyncio.sleep(0)

    try:
        asyncio.run(scenario())
    finally:
        player.mpv.terminate()
        player.close()
