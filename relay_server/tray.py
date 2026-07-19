"""Windows system-tray front end for the relay server.

Double-clicking the frozen ``upscale-relay-server-gui.exe`` starts the server
with the last-saved configuration and drops an icon in the notification area.
The tray menu opens a configuration pane (execution provider, control port,
media library folder, models folder, mDNS) that restarts the listeners in place
when applied.

The asyncio ``RelayServer`` and the Qt event loop are married with qasync, the
same integration the desktop client uses. ``relay_server.server`` stays free of
any Qt import so the headless ``relay-server`` CLI is unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .gui_settings import ServerSettings, available_ep_choices
from .server import RelayServer

log = logging.getLogger("relay.tray")

_APP_NAME = "Upscale Relay Server"

# Kept alive while file logging is enabled: faulthandler holds this file's fd,
# so reconfiguration redirects faulthandler before closing it.
_diagnostics_log = None
_diagnostics_handler = None
_console_stderr = None


class RuntimeSetupDialog(QDialog):
    """Non-modal first-run progress for the large NVIDIA runtime download."""

    status = Signal(str)
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{_APP_NAME} — NVIDIA setup")
        self.setMinimumWidth(520)
        self._process = None
        self.cancelled = False
        self.failed = False

        self.label = QLabel(
            "Installing the pinned TensorRT/CUDA runtime. This one-time "
            "download is several gigabytes and may take a while."
        )
        self.label.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignRight)
        self.status.connect(self._set_status)

    def _set_status(self, line: str) -> None:
        if line:
            self.label.setText(line[-500:])

    def set_process(self, process) -> None:
        self._process = process
        if self.cancelled and process.poll() is None:
            process.terminate()

    def cancel(self) -> None:
        self.cancelled = True
        self.label.setText("Cancelling setup…")
        self.cancel_button.setEnabled(False)
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def reject(self) -> None:
        # Treat the title-bar close gesture exactly like the visible Cancel
        # button; never leave a several-GB installer running invisibly.
        if self.failed:
            self.close_after_failure()
        else:
            self.cancel()

    def show_failure(self, detail: str) -> None:
        self.failed = True
        self.progress.hide()
        self.label.setText(
            "NVIDIA runtime setup failed:\n\n"
            f"{detail or 'No error detail was reported.'}\n\n"
            f"Full log: {diagnostics_log_path()}"
        )
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.cancel_button.clicked.disconnect()
        self.cancel_button.clicked.connect(self.close_after_failure)

    def close_after_failure(self, _checked: bool = False) -> None:
        self.hide()
        self.closed.emit()


async def ensure_runtime_gui() -> tuple[bool, RuntimeSetupDialog | None]:
    """Install the external runtime without blocking Qt's event loop."""
    from .runtime_bootstrap import activate_runtime, run_installer_process

    if activate_runtime():
        return True, None

    dialog = RuntimeSetupDialog()
    dialog.show()
    last_line = ""

    def report(line: str) -> None:
        nonlocal last_line
        if line:
            last_line = line
        log.info("runtime setup: %s", line)
        dialog.status.emit(line)

    result = await asyncio.to_thread(
        run_installer_process, report, dialog.set_process,
    )
    if result == 0 and activate_runtime():
        dialog.hide()
        dialog.deleteLater()
        return True, None
    if dialog.cancelled:
        dialog.hide()
    else:
        dialog.show_failure(last_line)
        closed = asyncio.get_running_loop().create_future()

        def finish_close() -> None:
            if not closed.done():
                closed.set_result(None)

        dialog.closed.connect(finish_close)
        await closed
    return False, dialog


def diagnostics_log_path() -> Path:
    """Return the visible, user-owned log path used by the tray GUI."""
    override = os.environ.get("RELAY_GUI_LOG_DIR")
    documents = override or QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    base = Path(documents) if documents else Path.home() / "Documents"
    return base / "upscale-relay-server.log"


def _open_diagnostics_log():
    """A writable log file for the windowed (frozen) build, which has no console.

    Returns an open text file in the user's Documents directory (or ``None``
    if it cannot be created). ``sys.stderr`` is ``None`` in a --windowed
    PyInstaller binary, so faulthandler and logging need a real file when file
    logging is enabled.
    """
    log_path = diagnostics_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(log_path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return None


def configure_file_logging(enabled: bool) -> Path | None:
    """Apply the GUI file-logging preference immediately."""
    import faulthandler

    global _diagnostics_log, _diagnostics_handler

    root = logging.getLogger()
    if _diagnostics_handler is not None:
        root.removeHandler(_diagnostics_handler)
        _diagnostics_handler.flush()
        _diagnostics_handler.close()

    if _diagnostics_log is not None:
        faulthandler.disable()
        if sys.stderr is _diagnostics_log:
            sys.stderr = _console_stderr
        _diagnostics_log.close()

    _diagnostics_log = None
    _diagnostics_handler = None

    if not enabled:
        if _console_stderr is not None:
            try:
                faulthandler.enable(file=_console_stderr)
            except (RuntimeError, ValueError):
                pass
        return None

    stream = _open_diagnostics_log()
    if stream is None:
        return None

    _diagnostics_log = stream
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"
    ))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _diagnostics_handler = handler

    if _console_stderr is None:
        sys.stderr = stream
    try:
        faulthandler.enable(file=stream)
    except (RuntimeError, ValueError):
        pass
    return diagnostics_log_path()


def make_icon() -> QIcon:
    """A self-contained tray icon drawn at runtime (no bundled asset file)."""
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2d7d9a"))
    painter.drawEllipse(4, 4, 56, 56)
    font = QFont()
    font.setBold(True)
    font.setPointSize(30)
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(pix.rect(), Qt.AlignCenter, "U")
    painter.end()
    return QIcon(pix)


class ServerController:
    """Owns the current ``RelayServer`` and rebuilds it on a config change.

    Port, execution provider, and library/models folders are all
    construction-time parameters of ``RelayServer``, so applying new settings
    means tearing the running listeners down and starting a fresh instance.
    """

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.server: RelayServer | None = None

    @property
    def running(self) -> bool:
        return self.server is not None

    async def start(self) -> None:
        """(Re)start the server from the persisted settings.

        Raises whatever ``RelayServer`` construction/startup raises (e.g. an
        invalid library folder, or a port already in use) after ensuring any
        previously running instance is stopped.
        """
        await self.stop()
        s = self.settings
        server = RelayServer(
            s.models_dir,
            s.port,
            ep=s.ep,
            stats_interval=2.0 if s.file_logging else None,
            library_root=s.library_dir or None,
            mdns=s.mdns,
        )
        await server.start()
        self.server = server

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None


def _folder_row(edit: QLineEdit, dialog: QWidget) -> QWidget:
    """A line edit paired with a Browse… button that picks a directory."""
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    button = QPushButton("Browse…")

    def browse() -> None:
        chosen = QFileDialog.getExistingDirectory(dialog, "Select folder", edit.text())
        if chosen:
            edit.setText(chosen)

    button.clicked.connect(browse)
    layout.addWidget(button)
    return row


class ConfigDialog(QDialog):
    """Non-modal configuration pane.

    Non-modal (``show()``, never ``exec()``) so the qasync loop is never
    re-entered from a nested modal loop — the same discipline the desktop
    client follows. ``applied`` fires after the edited values are persisted;
    the tray app listens for it to restart the server.
    """

    applied = Signal()

    def __init__(self, settings: ServerSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle(f"{_APP_NAME} — Configuration")

        self.ep_combo = QComboBox()
        self.ep_combo.addItems(available_ep_choices())
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.library_edit = QLineEdit()
        self.library_edit.setPlaceholderText("(none — no media library exposed)")
        self.models_edit = QLineEdit()
        self.mdns_check = QCheckBox("Advertise on the LAN via mDNS/DNS-SD")
        self.logging_check = QCheckBox(
            f"Write server log to {diagnostics_log_path()}"
        )

        form = QFormLayout(self)
        form.addRow("Execution provider:", self.ep_combo)
        form.addRow("Control port:", self.port_spin)
        form.addRow("Media library folder:", _folder_row(self.library_edit, self))
        form.addRow("Models folder:", _folder_row(self.models_edit, self))
        form.addRow("", self.mdns_check)
        form.addRow("", self.logging_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Apply | QDialogButtonBox.Close
        )
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._on_apply)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.hide)
        form.addRow(buttons)

        self.load()

    def load(self) -> None:
        """Populate the fields from the persisted settings."""
        s = self._settings
        self.ep_combo.setCurrentText(s.ep)
        self.port_spin.setValue(s.port)
        self.library_edit.setText(s.library_dir)
        self.models_edit.setText(s.models_dir)
        self.mdns_check.setChecked(s.mdns)
        self.logging_check.setChecked(s.file_logging)

    def _on_apply(self) -> None:
        s = self._settings
        s.ep = self.ep_combo.currentText()
        s.port = self.port_spin.value()
        s.library_dir = self.library_edit.text().strip()
        s.models_dir = self.models_edit.text().strip()
        s.mdns = self.mdns_check.isChecked()
        s.file_logging = self.logging_check.isChecked()
        self.applied.emit()


class TrayApp:
    """The tray icon, its menu, and the config pane wired to the controller."""

    def __init__(self, settings: ServerSettings):
        self.settings = settings
        self.controller = ServerController(settings)
        self.dialog: ConfigDialog | None = None

        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip(_APP_NAME)
        # Held on the instance: setContextMenu does not give Qt Python-side
        # ownership, so a local would be garbage-collected out from under it.
        self._menu = menu = QMenu()
        self._configure_action = QAction("Configure…", menu)
        self._configure_action.triggered.connect(self.open_config)
        self._restart_action = QAction("Restart server", menu)
        self._restart_action.triggered.connect(lambda: asyncio.ensure_future(self.restart()))
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(lambda: asyncio.ensure_future(self.quit()))
        menu.addAction(self._configure_action)
        menu.addAction(self._restart_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Start the server; on failure notify and open the config pane."""
        try:
            await self.controller.start()
        except Exception as err:  # bad folder, port in use, EP unavailable…
            log.warning("server start failed: %r", err)
            self._notify(f"Could not start: {err}", error=True)
            self.open_config()
            return
        self._notify(f"Listening on port {self.settings.port}")

    async def restart(self) -> None:
        try:
            await self.controller.start()
        except Exception as err:
            log.warning("server restart failed: %r", err)
            self._notify(f"Could not start: {err}", error=True)
            return
        self._notify(f"Restarted on port {self.settings.port}")

    async def quit(self) -> None:
        await self.controller.stop()
        QApplication.quit()

    # -- ui slots -------------------------------------------------------------

    def open_config(self) -> None:
        if self.dialog is None:
            self.dialog = ConfigDialog(self.settings)
            self.dialog.applied.connect(self._settings_applied)
        self.dialog.load()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _settings_applied(self) -> None:
        path = configure_file_logging(self.settings.file_logging)
        if self.settings.file_logging and path is None:
            self._notify("Could not open the selected Documents log file", error=True)
        elif path is not None:
            log.info("file logging enabled: %s", path)
        asyncio.ensure_future(self.restart())

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_config()

    def _notify(self, message: str, *, error: bool = False) -> None:
        icon = QSystemTrayIcon.Critical if error else QSystemTrayIcon.Information
        if self.tray.supportsMessages():
            self.tray.showMessage(_APP_NAME, message, icon, 5000)
        log.info("%s", message)


def setup_diagnostics(file_logging: bool = True) -> None:
    """Enable faulthandler + logging without assuming a console exists.

    Native faults (libav/ORT/TRT) kill the process silently otherwise — the
    faulting thread's Python stack is printed instead. In a --windowed frozen
    build there is no console: ``sys.stderr`` is ``None`` and both
    ``faulthandler.enable()`` and ``logging`` default to it, so a bare
    ``faulthandler.enable()`` raises "sys.stderr is None" and the app never
    starts. Fall back to a log file, which also makes such failures diagnosable.
    """
    import faulthandler

    global _console_stderr
    _console_stderr = sys.stderr

    if _console_stderr is not None:
        try:
            faulthandler.enable(file=_console_stderr)
        except (RuntimeError, ValueError):
            pass
        logging.basicConfig(
            level=logging.INFO,
            stream=_console_stderr,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    configure_file_logging(file_logging)
    try:
        from .crashinfo import install as _install_crashinfo

        _install_crashinfo()
    except Exception:
        pass

def main() -> None:
    # Frozen-build smoke hook: reaching here means the entry script imported
    # relay_server.tray (and its deps) successfully, so exit 0 without any UI.
    # A --windowed exe has no stdout, so signal via the exit code only — do not
    # print. This is what CI runs to prove the binary isn't missing modules.
    if "--check" in sys.argv[1:]:
        return

    settings = ServerSettings()
    setup_diagnostics(settings.file_logging)

    from qasync import QEventLoop

    app = QApplication(sys.argv)
    app.setApplicationName(_APP_NAME)
    # A tray-only app: closing the config pane must not exit the process.
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None, _APP_NAME,
            "No system tray is available on this desktop.\n"
            "Run the headless 'relay-server' command instead.",
        )
        sys.exit(1)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        runtime_ok, setup_dialog = loop.run_until_complete(ensure_runtime_gui())
        if not runtime_ok:
            return

        tray = TrayApp(settings)
        # start() never blocks (it awaits the listeners binding, then returns);
        # run the initial startup, then hand control to the tray event loop.
        loop.run_until_complete(tray.start())
        loop.run_forever()


if __name__ == "__main__":
    main()
