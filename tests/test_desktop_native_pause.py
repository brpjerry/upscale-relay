"""A pause made through an input.conf binding is the user's pause intent.

mpv pauses itself for each epoch's load-time hold and restores the pause
value it had before loadfile whenever a file stops; neither is the user's.
"""
import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")
pytest.importorskip("mpv")

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from desktop_client.main_window import MainWindow
from desktop_client.options import DesktopOptions
from relay_server.server import RelayServer
from upscale_cli.sample import make_sample
from ports import free_port_pair
from qt_helpers import playback_loop


def test_input_conf_pause_survives_seek_restart_and_fallback(tmp_path, monkeypatch):
    source_path = tmp_path / "a.mkv"
    make_sample(str(source_path), frames=600, width=64, height=64, fps=10)
    input_conf = tmp_path / "input.conf"
    input_conf.write_text("P cycle pause\n")
    app = QApplication.instance() or QApplication([])
    scope = "test-desktop-native-pause"
    QSettings("upscale-relay", scope).clear()
    window = MainWindow(options=DesktopOptions(
        headless=True, no_hwdec=True, settings_scope=scope, input_conf_path=input_conf,
        log_root=tmp_path / "logs", discovery=False,
    ))
    monkeypatch.setattr(window, "screen", lambda: SimpleNamespace(size=lambda: QSize(64, 64), devicePixelRatio=lambda: 1))
    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    window.settings.quality_tier = "lossless-ffv1"
    window.show()
    player = window.player
    flashes = []  # the pause/play feedback, once per change of intent
    monkeypatch.setattr(window.video_overlay, "flash", lambda _icon: flashes.append(window._paused))

    async def wait_for(predicate, timeout=20):
        async with asyncio.timeout(timeout):
            while not predicate():
                assert not errors, errors
                await asyncio.sleep(0.05)

    def press_binding():
        # The view forwards keys it does not reserve to mpv, which runs the
        # user's binding itself; the application never sees the command.
        player.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_P, Qt.ShiftModifier, "P"))

    async def native_pause(paused):
        press_binding()
        await wait_for(lambda: player.mpv.pause is paused)
        await wait_for(lambda: window._paused is paused, timeout=3)
        assert player._caller_paused is paused
        assert window.play_btn.toolTip() == ("Play (Space)" if paused else "Pause (Space)")

    async def settled():
        await wait_for(lambda: player._epoch_released and window._pending_seek_s is None)
        await asyncio.sleep(0.5)  # a resumed epoch would move on meanwhile

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair(), library_root=str(tmp_path))
        await server.start()
        try:
            window.host_edit.setText(f"127.0.0.1:{server.port}")
            await window.on_connect()
            await window._start_session(str(source_path), "uplink")
            await wait_for(lambda: player._epoch_released and window._position_s > 1)
            assert not window._paused
            await native_pause(True)

            # A relay seek reloads the epoch under a load-time hold, then
            # releases it with the caller's intent: still paused.
            await window._seek_to_seconds(8)
            await settled()
            assert player.mpv.pause, (window._paused, player._caller_paused)
            assert window._paused and abs(window._position_s - 8) < 1

            # A settings restart opens a new session from the snapshot.
            await window._restart_for_playback_setting()
            await settled()
            assert player.mpv.pause and window._paused
            assert abs(window._position_s - 8) < 1

            # Resuming through the binding is intent too: the next seek plays.
            await native_pause(False)
            await window._seek_to_seconds(20)
            await wait_for(lambda: player._epoch_released and window._position_s > 21)
            assert not player.mpv.pause and not window._paused

            # Local fallback keeps a binding's pause, and local playback
            # follows the binding the same way.
            await native_pause(True)
            await window._run_transition(window._fallback)
            await wait_for(lambda: window._session_source == "local" and player._epoch_released)
            await asyncio.sleep(0.3)
            assert player.mpv.pause and window._paused
            await native_pause(False)
            await native_pause(True)

            # The toolbar and Space still toggle the adopted state, once.
            await window.on_play_pause()
            await wait_for(lambda: player.mpv.pause is False)
            await window.on_play_pause()
            await wait_for(lambda: player.mpv.pause is True)
            await asyncio.sleep(0.3)
            assert window._paused
            # Neither the load-time holds nor the pause mpv restores when a
            # file stops were taken for the user's.
            assert flashes == [True, False, True, False, True, False, True]
            assert not errors, errors
            await window.on_stop()
        finally:
            window._closing = True
            await window._run_transition(window._teardown_session)
            if window.client:
                await window.client.close()
                window.client = None
            await window.discovery.close()
            window.client_log.close()
            player.stop()
            await asyncio.sleep(0.2)
            await server.stop()

    try:
        with playback_loop(app) as loop:
            loop.run_until_complete(scenario())
    finally:
        window._close_ready = True
        player._free_render_ctx()
        player.mpv.terminate()
        window.close()
