"""Offscreen coverage for the Windows server tray GUI."""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")  # server tray GUI is an optional extra
pytest.importorskip("qasync")

import socket

from PySide6.QtWidgets import QApplication

from relay_server.gui_settings import ServerSettings
from relay_server.tray import ConfigDialog, ServerController, TrayApp, make_icon

_next_port = [0]


def free_port_pair() -> int:
    """Find p such that p and p+1 are both free (RelayServer binds both).

    Walks a private range rather than check-then-use on ephemeral ports —
    the same approach as tests/test_streaming.py.
    """
    import random

    if _next_port[0] == 0:
        _next_port[0] = random.randrange(40000, 60000, 2)
    for _ in range(200):
        p = _next_port[0]
        _next_port[0] += 2
        try:
            with socket.socket() as s1, socket.socket() as s2:
                s1.bind(("127.0.0.1", p))
                s2.bind(("127.0.0.1", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def settings():
    s = ServerSettings(scope="test-server-tray")
    # Start every test from a known state regardless of prior runs.
    s.ep = "auto"
    s.port = 8590
    s.library_dir = ""
    s.models_dir = "models"
    s.mdns = True
    return s


def test_settings_roundtrip(settings):
    settings.ep = "dml"
    settings.port = 9001
    settings.library_dir = "D:/media"
    settings.models_dir = "D:/models"
    settings.mdns = False

    fresh = ServerSettings(scope="test-server-tray")
    assert fresh.ep == "dml"
    assert fresh.port == 9001
    assert fresh.library_dir == "D:/media"
    assert fresh.models_dir == "D:/models"
    assert fresh.mdns is False


def test_settings_reject_unknown_ep(settings):
    settings._qs.setValue("server/ep", "bogus")
    assert ServerSettings(scope="test-server-tray").ep == "auto"


def test_make_icon_is_non_null(app):
    assert not make_icon().isNull()


def test_config_dialog_load_and_apply_persists(app, settings):
    settings.ep = "cuda"
    settings.port = 8600
    settings.library_dir = "C:/lib"

    dialog = ConfigDialog(settings)
    try:
        assert dialog.ep_combo.currentText() == "cuda"
        assert dialog.port_spin.value() == 8600
        assert dialog.library_edit.text() == "C:/lib"

        applied = []
        dialog.applied.connect(lambda: applied.append(True))
        dialog.ep_combo.setCurrentText("cpu")
        dialog.port_spin.setValue(8700)
        dialog.library_edit.setText("C:/other")
        dialog.models_edit.setText("C:/models")
        dialog._on_apply()

        assert applied == [True]
        fresh = ServerSettings(scope="test-server-tray")
        assert fresh.ep == "cpu"
        assert fresh.port == 8700
        assert fresh.library_dir == "C:/other"
        assert fresh.models_dir == "C:/models"
    finally:
        dialog.deleteLater()


def test_controller_start_stop_binds_and_releases_port(app, settings, tmp_path):
    settings.models_dir = str(tmp_path)  # empty models dir is fine
    settings.port = free_port_pair()
    controller = ServerController(settings)

    async def scenario():
        await controller.start()
        assert controller.running
        # Applying a new port rebinds; the old listeners must be released so
        # the new instance can bind without EADDRINUSE.
        settings.port = free_port_pair()
        await controller.start()
        assert controller.running
        await controller.stop()
        assert not controller.running

    asyncio.run(scenario())


def test_controller_start_raises_on_bad_library(app, settings, tmp_path):
    settings.models_dir = str(tmp_path)
    settings.port = free_port_pair()
    settings.library_dir = str(tmp_path / "does-not-exist")
    controller = ServerController(settings)

    async def scenario():
        with pytest.raises(ValueError):
            await controller.start()
        assert not controller.running

    asyncio.run(scenario())


def test_tray_app_start_failure_opens_config(app, settings, tmp_path, monkeypatch):
    settings.models_dir = str(tmp_path)
    settings.port = free_port_pair()
    settings.library_dir = str(tmp_path / "missing")
    tray = TrayApp(settings)
    opened = []
    monkeypatch.setattr(tray, "open_config", lambda: opened.append(True))

    async def scenario():
        await tray.start()

    asyncio.run(scenario())
    assert opened == [True]
    tray.tray.hide()
