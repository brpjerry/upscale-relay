"""A failing local/SMB source read ends desktop playback with a visible error."""
import asyncio
import os
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
from relay_client_core import client as client_module
from relay_server.server import RelayServer
from upscale_cli.sample import make_sample
from ports import free_port_pair
from qt_helpers import playback_loop
from test_client_source_failure import FailingTrack


def test_source_read_failure_stops_playback_with_the_error(tmp_path, monkeypatch):
    source_path = tmp_path / "a.mkv"
    make_sample(str(source_path), frames=240, width=64, height=64, fps=10)
    monkeypatch.setattr(client_module, "VideoTrack", FailingTrack)
    app = QApplication.instance() or QApplication([])
    scope = "test-desktop-source-failure"
    QSettings("upscale-relay", scope).clear()
    window = MainWindow(options=DesktopOptions(
        headless=True, no_hwdec=True, settings_scope=scope,
        log_root=tmp_path / "logs", discovery=False,
    ))
    monkeypatch.setattr(window, "screen", lambda: SimpleNamespace(size=lambda: QSize(64, 64), devicePixelRatio=lambda: 1))
    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    window.settings.quality_tier = "lossless-ffv1"
    window.show()

    async def wait_for(predicate):
        async with asyncio.timeout(20):
            while not predicate():
                await asyncio.sleep(0.05)

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair())
        await server.start()
        try:
            window.host_edit.setText(f"127.0.0.1:{server.port}")
            await window.on_connect()
            await window._start_session(str(source_path), "uplink")
            failed_client = window.client
            assert window._session_source == "uplink"
            # Before the fix nothing was reported: playback sat buffering.
            await wait_for(lambda: errors)
            assert errors == [("Playback failed", "Could not read the source file: SMB source read failed")]
            # The playback stops through the ordinary confirmed teardown and a
            # fresh control connection replaces the failed session's.
            await wait_for(lambda: window._session_path is None and window.client is not failed_client)
            assert window.client is not None and window.client.connected
            assert errors == [("Playback failed", "Could not read the source file: SMB source read failed")]
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
