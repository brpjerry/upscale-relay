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

from relay_server import autostart
from relay_server.gui_settings import EP_CHOICES, ServerSettings, available_ep_choices
from relay_server.tray import (
    ConfigDialog,
    RuntimeSetupDialog,
    ServerController,
    TrayApp,
    ensure_runtime_gui,
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


_TEST_RUN_KEY = r"Software\upscale-relay-tests\Run"


@pytest.fixture(autouse=True)
def isolated_autostart(monkeypatch):
    """Point every test at a private Run key.

    ConfigDialog._on_apply() writes the autostart registration, so without
    this the dialog tests would edit the user's real
    HKCU\\...\\CurrentVersion\\Run entry.
    """
    monkeypatch.setattr(autostart, "_RUN_KEY", _TEST_RUN_KEY)
    yield
    if sys.platform == "win32":
        import winreg

        for key in (_TEST_RUN_KEY, r"Software\upscale-relay-tests"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass


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


def test_autostart_launch_command_is_quoted():
    command = autostart.launch_command()
    assert command.startswith('"')


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry only")
def test_autostart_registry_roundtrip():
    import winreg

    assert autostart.is_enabled() is False
    autostart.set_enabled(True)
    assert autostart.is_enabled() is True
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart._RUN_KEY) as key:
        value, kind = winreg.QueryValueEx(key, autostart._VALUE_NAME)
    assert value == autostart.launch_command()
    assert kind == winreg.REG_SZ
    autostart.set_enabled(False)
    assert autostart.is_enabled() is False
    autostart.set_enabled(False)  # disabling twice must not raise


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry only")
def test_config_dialog_autostart_checkbox(app, settings):
    autostart.set_enabled(True)
    dialog = ConfigDialog(settings)
    try:
        assert dialog.autostart_check.isChecked()
        dialog.autostart_check.setChecked(False)
        dialog._on_apply()
        assert autostart.is_enabled() is False

        dialog.autostart_check.setChecked(True)
        dialog._on_apply()
        assert autostart.is_enabled() is True
    finally:
        dialog.deleteLater()


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


def test_runtime_setup_failure_shows_detail_and_closes(app):
    dialog = RuntimeSetupDialog()
    closed = []
    dialog.closed.connect(lambda: closed.append(True))
    dialog.show_failure("Unable to locate finder for 'pip._vendor.distlib'")

    assert dialog.failed
    assert "pip._vendor.distlib" in dialog.label.text()
    assert "upscale-relay-server.log" in dialog.label.text()
    dialog.reject()
    assert closed == [True]
    assert not dialog.isVisible()


def test_runtime_setup_failure_coroutine_returns_after_close(app, monkeypatch):
    from relay_server import runtime_bootstrap

    monkeypatch.setattr(runtime_bootstrap, "activate_runtime", lambda: False)

    def fail_installer(on_line, _on_process):
        on_line("Unable to locate finder for 'pip._vendor.distlib'")
        return 1

    monkeypatch.setattr(runtime_bootstrap, "run_installer_process", fail_installer)

    async def scenario():
        setup = asyncio.create_task(ensure_runtime_gui())
        dialog = None
        for _ in range(100):
            await asyncio.sleep(0.01)
            dialog = next((
                widget for widget in QApplication.topLevelWidgets()
                if isinstance(widget, RuntimeSetupDialog) and widget.failed
            ), None)
            if dialog is not None:
                break
        assert dialog is not None
        assert "pip._vendor.distlib" in dialog.label.text()
        dialog.close_after_failure()
        ok, returned_dialog = await asyncio.wait_for(setup, timeout=1)
        assert ok is False
        assert returned_dialog is dialog

    asyncio.run(scenario())


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
    events = []
    callback = events.append
    controller.event_callback = callback

    async def scenario():
        await controller.start()
        assert controller.running
        assert controller.server.stats_interval == 2.0
        # Connection/playback events must reach the tray callback on every
        # (re)started instance.
        assert controller.server.event_callback is callback
        # Applying a new port rebinds; the old listeners must be released so
        # the new instance can bind without EADDRINUSE.
        settings.port = free_port_pair()
        settings.file_logging = False
        await controller.start()
        assert controller.running
        assert controller.server.stats_interval is None
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
    assert tray.controller.event_callback == tray._server_event
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
