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
    s.library_dirs = []
    s.models_dir = "models"
    s.mdns = True
    s.file_logging = True
    return s


def test_settings_roundtrip(settings):
    provider = next((choice for choice in EP_CHOICES if choice != "auto"), "auto")
    settings.ep = provider
    settings.port = 9001
    settings.library_dirs = ["D:/media", "E:/shows"]
    settings.models_dir = "D:/models"
    settings.mdns = False
    settings.file_logging = False

    fresh = ServerSettings(scope="test-server-tray")
    assert fresh.ep == provider
    assert fresh.port == 9001
    assert fresh.library_dirs == ["D:/media", "E:/shows"]
    assert fresh.library_dir == "D:/media"
    assert fresh.models_dir == "D:/models"
    assert fresh.mdns is False
    assert fresh.file_logging is False


def test_settings_migrates_legacy_single_library_value():
    settings = ServerSettings(scope="test-server-tray-legacy-library")
    settings._qs.remove("server/library_dirs")
    settings._qs.setValue("server/library_dir", "D:/legacy-media")

    fresh = ServerSettings(scope="test-server-tray-legacy-library")
    assert fresh.library_dirs == ["D:/legacy-media"]

    fresh.library_dirs = ["D:/movies", "E:/shows"]
    assert fresh._qs.value("server/library_dir", None) is None
    assert ServerSettings(scope="test-server-tray-legacy-library").library_dirs == [
        "D:/movies", "E:/shows",
    ]


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
    monkeypatch.setattr(runtime_bootstrap, "source_runtime_ready", lambda ep: False)

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


def test_usable_source_runtime_skips_managed_installation(app, monkeypatch):
    from relay_server import runtime_bootstrap
    monkeypatch.delattr(sys, "frozen", raising=False)
    checked = []
    monkeypatch.setattr(runtime_bootstrap, "source_runtime_ready", lambda ep: checked.append(ep) or True)
    monkeypatch.setattr(runtime_bootstrap, "activate_runtime", lambda: pytest.fail("must retain source runtime"))
    assert asyncio.run(ensure_runtime_gui("cuda")) == (True, None)
    assert checked == ["cuda"]


def test_frozen_gui_always_uses_managed_runtime(app, monkeypatch):
    from relay_server import runtime_bootstrap
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_bootstrap, "source_runtime_ready", lambda ep: pytest.fail("source check in frozen app"))
    monkeypatch.setattr(runtime_bootstrap, "activate_runtime", lambda: True)
    assert asyncio.run(ensure_runtime_gui("auto")) == (True, None)


def test_config_dialog_load_and_apply_persists(app, settings):
    provider = next((choice for choice in EP_CHOICES if choice != "auto"), "auto")
    settings.ep = provider
    settings.port = 8600
    settings.library_dirs = ["C:/lib", "D:/shows"]

    dialog = ConfigDialog(settings)
    try:
        assert dialog.ep_combo.currentText() == provider
        assert dialog.port_spin.value() == 8600
        assert [
            dialog.library_list.item(index).text()
            for index in range(dialog.library_list.count())
        ] == ["C:/lib", "D:/shows"]
        assert dialog.logging_check.isChecked()

        applied = []
        dialog.applied.connect(lambda: applied.append(True))
        applied_provider = next(
            (choice for choice in available_ep_choices() if choice != provider),
            provider,
        )
        dialog.ep_combo.setCurrentText(applied_provider)
        dialog.port_spin.setValue(8700)
        dialog.library_list.clear()
        dialog.library_list.addItems(["C:/other", "E:/movies"])
        dialog.models_edit.setText("C:/models")
        dialog.logging_check.setChecked(False)
        dialog._on_apply()

        assert applied == [True]
        fresh = ServerSettings(scope="test-server-tray")
        assert fresh.ep == applied_provider
        assert fresh.port == 8700
        assert fresh.library_dirs == ["C:/other", "E:/movies"]
        assert fresh.models_dir == "C:/models"
        assert fresh.file_logging is False
    finally:
        dialog.deleteLater()


def test_config_dialog_adds_and_removes_library_folders(app, settings, monkeypatch):
    dialog = ConfigDialog(settings)
    try:
        choices = iter(["C:/movies", "D:/shows", "C:/movies"])
        monkeypatch.setattr(
            "relay_server.tray.QFileDialog.getExistingDirectory",
            lambda *_args: next(choices),
        )
        dialog._add_library_folder()
        dialog._add_library_folder()
        dialog._add_library_folder()  # duplicate is ignored
        assert [
            dialog.library_list.item(index).text()
            for index in range(dialog.library_list.count())
        ] == ["C:/movies", "D:/shows"]

        dialog.library_list.item(0).setSelected(True)
        dialog._remove_library_folders()
        assert dialog.library_list.count() == 1
        assert dialog.library_list.item(0).text() == "D:/shows"
    finally:
        dialog.deleteLater()


def test_controller_start_stop_binds_and_releases_port(app, settings, tmp_path):
    settings.models_dir = str(tmp_path)  # empty models dir is fine
    settings.port = free_port_pair()
    settings.mdns = False  # Keep this listener/config test independent of LAN name collisions.
    first_library = tmp_path / "movies"
    second_library = tmp_path / "shows"
    first_library.mkdir()
    second_library.mkdir()
    settings.library_dirs = [str(first_library), str(second_library)]
    controller = ServerController(settings)
    events = []
    callback = events.append
    controller.event_callback = callback

    async def scenario():
        await controller.start()
        assert controller.running
        assert controller.server.stats_interval == 2.0
        assert controller.server.library.roots == (
            first_library.resolve(), second_library.resolve(),
        )
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


def test_controller_serializes_overlapping_restarts(settings, monkeypatch):
    instances = []

    async def scenario():
        started, finish_start = asyncio.Event(), asyncio.Event()

        class Server:
            def __init__(self, models, port, **kwargs):
                self.port, self.running = port, False
                instances.append(self)

            async def start(self):
                started.set()
                await finish_start.wait()
                self.running = True

            async def stop(self):
                self.running = False

        monkeypatch.setattr("relay_server.tray.RelayServer", Server)
        controller = ServerController(settings)
        first = asyncio.create_task(controller.start())
        await started.wait()
        settings.port = 8690
        second = asyncio.create_task(controller.start())
        await asyncio.sleep(0)
        assert len(instances) == 1
        finish_start.set()
        await asyncio.gather(first, second)
        assert [server.port for server in instances if server.running] == [8690]
        await controller.stop()
        assert not any(server.running for server in instances)

    asyncio.run(scenario())


def test_config_port_reserves_media_port_and_explains_restart(app, settings):
    dialog = ConfigDialog(settings)
    try:
        dialog.port_spin.setValue(65535)
        assert dialog.port_spin.value() == 65534
        assert dialog.apply_button.text() == "Apply and restart"
        dialog.set_server_status("Restarting server…", "Control localhost:8590 · Media localhost:8591", True)
        assert not dialog.apply_button.isEnabled()
        assert "8591" in dialog.address_label.text()
    finally:
        dialog.deleteLater()


def test_tray_persists_failure_and_disables_restart_while_busy(app, settings, monkeypatch):
    tray = TrayApp(settings)
    tray.open_config()

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        attempts = []

        async def fail_start():
            attempts.append(True)
            entered.set()
            await finish.wait()
            raise OSError("port unavailable")

        monkeypatch.setattr(tray.controller, "start", fail_start)
        task = asyncio.create_task(tray.restart())
        await entered.wait()
        assert "Restarting" in tray.dialog.status_label.text()
        assert not tray.dialog.apply_button.isEnabled()
        assert not tray._restart_action.isEnabled()
        await tray.restart()
        assert len(attempts) == 1
        finish.set()
        await task
        assert "port unavailable" in tray.dialog.status_label.text()
        assert "port unavailable" in tray.tray.toolTip()
        assert tray.dialog.apply_button.isEnabled()
        assert tray._restart_action.isEnabled()

    try:
        asyncio.run(scenario())
    finally:
        tray.dialog.close()
        tray.tray.hide()


@pytest.mark.parametrize("cancel", [False, True])
def test_controller_cleans_up_unpublished_startup(settings, monkeypatch, cancel):
    instances = []

    async def scenario():
        started = asyncio.Event()

        class Server:
            def __init__(self, *args, **kwargs):
                self.running = False
                instances.append(self)

            async def start(self):
                self.running = True
                started.set()
                if cancel:
                    await asyncio.Event().wait()
                raise OSError("control port unavailable")

            async def stop(self):
                self.running = False

        monkeypatch.setattr("relay_server.tray.RelayServer", Server)
        controller = ServerController(settings)
        task = asyncio.create_task(controller.start())
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await task
        assert not controller.running
        assert not any(server.running for server in instances)

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


def test_diagnostic_rotation_bounds_backups_and_preserves_native_writer_fd(tmp_path, monkeypatch):
    import faulthandler
    from relay_server import tray

    real_stderr = sys.stderr
    was_enabled = faulthandler.is_enabled()
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setenv("RELAY_GUI_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(tray, "_LOG_MAX_BYTES", 512)
    try:
        tray.setup_diagnostics(True)
        stream = tray._diagnostics_log
        descriptor = stream.fileno()
        logger = logging.getLogger("relay.rotation-test")
        for index in range(20):
            logger.info("sample %d %s", index, "x" * 256)
        backups = list(tmp_path.glob("upscale-relay-server.log.*"))
        assert len(backups) == 3
        assert all(path.stat().st_size <= 512 for path in backups)
        assert stream.fileno() == descriptor
        assert sys.stderr is stream
        assert faulthandler.is_enabled()
        os.write(descriptor, b"native crash writer after rotation\n")
        faulthandler.dump_traceback(file=stream)
        contents = tray.diagnostics_log_path().read_text()
        assert "native crash writer after rotation" in contents
        assert "test_diagnostic_rotation" in contents
    finally:
        tray.configure_file_logging(False)
        faulthandler.disable()
        if was_enabled and real_stderr is not None:
            faulthandler.enable(file=real_stderr)
