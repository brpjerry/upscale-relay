"""Theme and custom-widget behavior the window layout relies on."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

import desktop_client.main_window as main_window
from desktop_client import theme
from desktop_client.options import DesktopOptions
from desktop_client.theme import Icons
from test_server_library_gui import FakePlayer

SCOPE = "test-desktop-theme"


@pytest.fixture()
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "PlayerView", FakePlayer)
    QSettings("upscale-relay", SCOPE).clear()
    result = main_window.MainWindow(options=DesktopOptions(
        headless=True, settings_scope=SCOPE,
        mpv_config_path=tmp_path / "mpv.conf", log_root=tmp_path / "logs",
    ))
    yield result
    result.client = None
    result.close()
    app.processEvents()
    theme.apply_theme(app, "auto")


def test_theme_mode_switches_palette_and_persists(window):
    app = QApplication.instance()
    window.show()
    window._set_theme_mode("dark")
    app.processEvents()  # synchronous test, outside coroutine context
    assert theme.current().dark
    assert app.palette().color(QPalette.Window) == QColor(theme.current().bg)
    assert window.palette().color(QPalette.WindowText) == QColor("#eef0f4")
    window._set_theme_mode("light")
    app.processEvents()
    assert not theme.current().dark
    assert window.pos_label.palette().color(QPalette.WindowText) == QColor("#15171b")
    assert window.settings.theme_mode == "light"


def test_icons_render_in_the_requested_colour():
    QApplication.instance() or QApplication([])
    image = theme.icon_pixmap(Icons.stop, "#ff0000", 24, 2.0).toImage()
    assert image.width() == 48
    assert image.pixelColor(24, 24) == QColor("#ff0000")
    assert image.pixelColor(1, 1).alpha() == 0


def test_settings_sheet_overlays_without_growing_the_window(window):
    window.resize(900, 600)
    window.show()
    QApplication.instance().processEvents()
    minimum = window.minimumSizeHint().width()
    window.playback_settings_toggle.setChecked(True)
    QApplication.instance().processEvents()
    assert window.playback_settings.isVisible()
    assert window.minimumSizeHint().width() == minimum
    sheet = window.playback_settings.geometry()
    assert sheet.right() == window._root.width() - 1
    assert sheet.bottom() < window.controls_panel.mapTo(window._root, window.controls_panel.rect().topLeft()).y()
    window.playback_settings.hide()
    assert not window.playback_settings_toggle.isChecked()


def test_connection_state_drives_the_top_bar(window):
    window.show()
    assert window.connect_btn.property("primary") is True
    assert window.host_edit.isVisible()
    window._show_connection("connected: box", connected=True)
    assert window.connect_btn.text() == "Disconnect"
    assert window.connect_btn.property("primary") is False
    assert not window.host_edit.isVisible()
    assert window.conn_label.text() == "connected: box"


def test_track_card_follows_its_button_and_escape_closes_it(window):
    from PySide6.QtGui import QKeyEvent
    window.show()
    window.tracks_btn.setChecked(True)
    assert window.track_panel.isVisible()
    assert window.audio_combo.isVisible()
    window.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not window.track_panel.isVisible()
    assert not window.tracks_btn.isChecked()


def test_status_messages_surface_in_the_subheading(window):
    window.statusBar().showMessage("seeking to 12.0s")
    assert window.subheading.text() == "seeking to 12.0s"
    window.statusBar().clearMessage()
    assert window.subheading.text() == "Not connected"
    assert window.statusBar().isHidden()
