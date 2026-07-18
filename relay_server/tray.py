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
import sys

from PySide6.QtCore import Qt, Signal
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
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QWidget,
)

from .gui_settings import EP_CHOICES, ServerSettings
from .server import RelayServer

log = logging.getLogger("relay.tray")

_APP_NAME = "Upscale Relay Server"


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
        self.ep_combo.addItems(EP_CHOICES)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.library_edit = QLineEdit()
        self.library_edit.setPlaceholderText("(none — no media library exposed)")
        self.models_edit = QLineEdit()
        self.mdns_check = QCheckBox("Advertise on the LAN via mDNS/DNS-SD")

        form = QFormLayout(self)
        form.addRow("Execution provider:", self.ep_combo)
        form.addRow("Control port:", self.port_spin)
        form.addRow("Media library folder:", _folder_row(self.library_edit, self))
        form.addRow("Models folder:", _folder_row(self.models_edit, self))
        form.addRow("", self.mdns_check)

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

    def _on_apply(self) -> None:
        s = self._settings
        s.ep = self.ep_combo.currentText()
        s.port = self.port_spin.value()
        s.library_dir = self.library_edit.text().strip()
        s.models_dir = self.models_edit.text().strip()
        s.mdns = self.mdns_check.isChecked()
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
            self.dialog.applied.connect(lambda: asyncio.ensure_future(self.restart()))
        self.dialog.load()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_config()

    def _notify(self, message: str, *, error: bool = False) -> None:
        icon = QSystemTrayIcon.Critical if error else QSystemTrayIcon.Information
        if self.tray.supportsMessages():
            self.tray.showMessage(_APP_NAME, message, icon, 5000)
        log.info("%s", message)


def main() -> None:
    # Native faults (libav/ORT/TRT) kill the process silently otherwise —
    # print the Python-level stack of the faulting thread instead.
    import faulthandler

    faulthandler.enable()
    try:
        from .crashinfo import install as _install_crashinfo

        _install_crashinfo()
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

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

    tray = TrayApp(ServerSettings())
    with loop:
        # start() never blocks (it awaits the listeners binding, then returns);
        # run the initial startup, then hand control to the tray event loop.
        loop.run_until_complete(tray.start())
        loop.run_forever()


if __name__ == "__main__":
    main()
