"""Real passthrough playback for resume, paused restart, seek, and autoplay.

RELAY_TEST_WAYLAND=1 with QT_QPA_PLATFORM=wayland also exercises the render API
on a real compositor. Ordinary CI uses the native headless mpv backend.
"""
import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")
pytest.importorskip("mpv")

from PySide6.QtCore import QSettings, QSize
from PySide6.QtWidgets import QApplication

from desktop_client.main_window import MainWindow
from desktop_client.options import DesktopOptions
from relay_server.server import RelayServer
from upscale_cli.sample import make_sample
from ports import free_port_pair
from qt_helpers import playback_loop


@pytest.mark.parametrize("source", ["uplink", "server_file"])
@pytest.mark.parametrize("demuxer", ["default", "lavf"])
def test_passthrough_resume_restart_seek_reopen_and_autoplay(tmp_path, monkeypatch, source, demuxer):
    source_path = tmp_path / "a.mkv"
    next_path = tmp_path / "b.mkv"
    make_sample(str(source_path), frames=1200, width=64, height=64, fps=10)
    make_sample(str(next_path), frames=120, width=64, height=64, fps=10)
    app = QApplication.instance() or QApplication([])
    scope = f"test-parity-playback-{source}-{demuxer}"
    QSettings("upscale-relay", scope).clear()
    window = MainWindow(options=DesktopOptions(
        headless=os.environ.get("RELAY_TEST_WAYLAND") != "1", no_hwdec=True,
        settings_scope=scope, log_root=tmp_path / "logs", discovery=False,
    ))
    if demuxer == "lavf":
        # Exercise the old-mpv FFV1 compatibility path on modern libmpv too;
        # absolute PTS and pause/seek behavior must agree across demuxers.
        window.player.mpv["demuxer"] = "lavf"
        window.player.mpv["demuxer-lavf-format"] = "matroska"
    monkeypatch.setattr(window, "screen", lambda: SimpleNamespace(size=lambda: QSize(64, 64), devicePixelRatio=lambda: 1))
    errors = []
    # Preserve native decoder/demuxer warnings in pytest's captured log. A
    # numeric end-file error alone cannot diagnose differences in distro mpv.
    window.player.log_message.connect(
        lambda level, prefix, message: logging.getLogger(__name__).warning(
            "mpv %s [%s]: %s", level, prefix, message.strip()))
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    window.settings.quality_tier = "lossless-ffv1"
    window.show()
    path = str(source_path) if source == "uplink" else "a.mkv"
    next_source_path = str(next_path) if source == "uplink" else "b.mkv"

    async def wait_for(predicate):
        async with asyncio.timeout(20):
            while not predicate():
                assert not errors, errors
                await asyncio.sleep(0.05)

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair(), library_root=str(tmp_path))
        await server.start()
        try:
            window.host_edit.setText(f"127.0.0.1:{server.port}")
            await window.on_connect()
            key = window._key_for(source, path)
            window.history.save(key, 12.0, 120.0)
            positions = []
            window.player.position_changed.connect(positions.append)
            await window._start_session(path, source)
            await wait_for(lambda: window._position_s >= 12 and window._pending_seek_s is None)
            assert positions and min(positions) >= 11.5
            assert window.player_status.isHidden()
            assert window.client.buffered_ms > 0  # diagnostics visibility never disables pacing
            await window.on_play_pause()
            before = window._position_s
            window.audio_delay.setValue(0.2)
            window.sub_delay.setValue(-0.1)
            window.player.select_subtitle(None)
            await window._restart_for_playback_setting()
            await wait_for(lambda: window.player._epoch_released and window._pending_seek_s is None)
            assert window._paused and window.player.mpv.pause
            assert window.player.mpv.sid is False
            assert abs(window._position_s - before) < 1
            assert window.player.mpv.audio_delay == pytest.approx(0.2)
            assert window.player.mpv.sub_delay == pytest.approx(-0.1)
            await window._seek_to_seconds(20)
            await wait_for(lambda: window._pending_seek_s is None)
            assert window._paused
            assert abs(window._position_s - 20) < 1
            window._save_history()
            await window.on_stop()
            await window._start_session(path, source)
            await wait_for(lambda: window._pending_seek_s is None and window._position_s >= 19)
            assert not window._paused
            await window._seek_to_seconds(119)
            await wait_for(lambda: window._session_path == next_source_path)
            assert window.history.entries[key].watched
            assert not errors
            await window.on_stop()
        finally:
            window._closing = True
            await window._run_transition(window._teardown_session)
            if window.client:
                await window.client.close()
                window.client = None
            await window.discovery.close()
            window.client_log.close()
            window.player.stop()
            await asyncio.sleep(0.2)
            await server.stop()

    try:
        with playback_loop(app) as loop:
            loop.run_until_complete(scenario())
    finally:
        window._close_ready = True
        window.player._free_render_ctx()
        window.player.mpv.terminate()
        window.close()
