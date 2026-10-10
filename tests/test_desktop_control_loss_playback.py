"""A control connection that dies mid-playback recovers without a manual reconnect.

Real server, real client core and real (headless) mpv: the control WebSocket
is dropped under a playing session, the way a suspend/resume or a Wi-Fi drop
leaves it. The teardown that follows cannot be acknowledged; the window used
to stop at "server teardown unconfirmed" until the user connected again.
"""
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
from relay_server.server import RelayServer
from upscale_cli.sample import make_sample
from ports import free_port_pair
from qt_helpers import playback_loop


def test_control_loss_mid_playback_ends_connected_and_usable(tmp_path, monkeypatch):
    source_path = tmp_path / "a.mkv"
    make_sample(str(source_path), frames=1200, width=64, height=64, fps=10)
    app = QApplication.instance() or QApplication([])
    scope = "test-desktop-control-loss-playback"
    QSettings("upscale-relay", scope).clear()
    window = MainWindow(options=DesktopOptions(
        headless=True, no_hwdec=True, settings_scope=scope,
        log_root=tmp_path / "logs", discovery=False,
    ))
    monkeypatch.setattr(window, "screen", lambda: SimpleNamespace(size=lambda: QSize(64, 64), devicePixelRatio=lambda: 1))
    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    logged = []
    monkeypatch.setattr(window.client_log, "record",
                        lambda event, **fields: logged.append((event, fields.get("session"))))
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
            await wait_for(lambda: window._position_s > 0.5)
            lost = window.client
            session_id = lost.session.session_id
            assert [s.id for s in server.sessions.values()].count(session_id)
            # No close frame, no teardown message: the socket just goes away.
            lost._ws._conn.transport.abort()
            await wait_for(lambda: errors)
            assert errors[0][0] == "Playback failed"
            await wait_for(lambda: window._session_path is None and not window._transitioning)
            # The server released the session on its own when the control
            # connection went; the replacement connection confirms that.
            assert session_id not in server.sessions
            assert [title for title, _message in errors if title != "Playback failed"] == []
            # It asked about the session it could not tear down, and was answered.
            assert [entry for entry in logged if entry[1] is not None] == [
                ("teardown_unconfirmed", session_id),
                ("release_wait", session_id),
                ("release_confirmed", session_id),
            ]
            assert window._unreleased_session is None
            assert window.client is not None and window.client is not lost
            assert window.client.connected
            assert window.connect_btn.text() == "Disconnect"
            assert window.conn_label.text().startswith("connected")
            # Usable: the same file plays again on the replacement connection.
            await window._start_session(str(source_path), "uplink")
            await wait_for(lambda: window._position_s > 0.5)
            assert window.client.session is not None
            assert [title for title, _message in errors if title != "Playback failed"] == []
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
