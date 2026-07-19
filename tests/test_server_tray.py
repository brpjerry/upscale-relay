"""Offscreen coverage for the Windows server tray GUI."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")  # server tray GUI is an optional extra
pytest.importorskip("qasync")

import socket

from PySide6.QtWidgets import QApplication

from relay_server.gui_settings import EP_CHOICES, ServerSettings, available_ep_choices
from relay_server.tray import (
    ConfigDialog,
    RuntimeSetupDialog,
    ServerController,
    TrayApp,
    make_icon,
)

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
    s.file_logging = True
    return s


def test_settings_roundtrip(settings):
    provider = next((choice for choice in EP_CHOICES if choice != "auto"), "auto")
    settings.ep = provider
    settings.port = 9001
    settings.library_dir = "D:/media"
    settings.models_dir = "D:/models"
    settings.mdns = False
    settings.file_logging = False

    fresh = ServerSettings(scope="test-server-tray")
    assert fresh.ep == provider
    assert fresh.port == 9001
    assert fresh.library_dir == "D:/media"
    assert fresh.models_dir == "D:/models"
    assert fresh.mdns is False
    assert fresh.file_logging is False


def test_settings_reject_unknown_ep(settings):
    settings._qs.setValue("server/ep", "bogus")
    assert ServerSettings(scope="test-server-tray").ep == "auto"


def test_make_icon_is_non_null(app):
    assert not make_icon().isNull()


def test_runtime_setup_close_cancels_installer(app):
    class FakeProcess:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    dialog = RuntimeSetupDialog()
    process = FakeProcess()
    dialog.set_process(process)
    dialog.reject()
    assert dialog.cancelled
    assert process.terminated


def test_config_dialog_load_and_apply_persists(app, settings):
    provider = next((choice for choice in EP_CHOICES if choice != "auto"), "auto")
    settings.ep = provider
    settings.port = 8600
    settings.library_dir = "C:/lib"

    dialog = ConfigDialog(settings)
    try:
        assert dialog.ep_combo.currentText() == provider
        assert dialog.port_spin.value() == 8600
        assert dialog.library_edit.text() == "C:/lib"
        assert dialog.logging_check.isChecked()

        applied = []
        dialog.applied.connect(lambda: applied.append(True))
        applied_provider = next(
            (choice for choice in available_ep_choices() if choice != provider),
            provider,
        )
        dialog.ep_combo.setCurrentText(applied_provider)
        dialog.port_spin.setValue(8700)
        dialog.library_edit.setText("C:/other")
        dialog.models_edit.setText("C:/models")
        dialog.logging_check.setChecked(False)
        dialog._on_apply()

        assert applied == [True]
        fresh = ServerSettings(scope="test-server-tray")
        assert fresh.ep == applied_provider
        assert fresh.port == 8700
        assert fresh.library_dir == "C:/other"
        assert fresh.models_dir == "C:/models"
        assert fresh.file_logging is False
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


def test_setup_diagnostics_writes_documents_log_without_stderr(tmp_path, monkeypatch):
    # A --windowed frozen build has sys.stderr is None; a bare
    # faulthandler.enable() would raise "sys.stderr is None" and the exe would
    # never start. setup_diagnostics() must fall back to a log file instead.
    import faulthandler

    from relay_server import tray

    real_stderr = sys.stderr  # captured before we blank it, for cleanup
    was_enabled = faulthandler.is_enabled()
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv("RELAY_GUI_LOG_DIR", str(tmp_path))
    tray._diagnostics_log = None
    tray._diagnostics_handler = None
    try:
        tray.setup_diagnostics(True)  # must not raise
        logging.getLogger("relay.test").info("documents log probe")
        tray._diagnostics_handler.flush()
        log_file = tmp_path / "upscale-relay-server.log"
        assert log_file.exists()
        assert "documents log probe" in log_file.read_text(encoding="utf-8")
        assert tray._diagnostics_log is not None
    finally:
        tray.configure_file_logging(False)
        # Restore faulthandler against the real stderr (still blanked here —
        # monkeypatch only undoes at teardown, after this finally runs).
        faulthandler.disable()
        if was_enabled and real_stderr is not None:
            faulthandler.enable(file=real_stderr)
