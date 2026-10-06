"""Theme and custom-widget behavior the window layout relies on."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QColor, QImage, QPalette, QWheelEvent
from PySide6.QtTest import QTest
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


def wait_until(condition, timeout_ms=4000):
    """Spin the event loop until ``condition()`` holds; animations run on the
    wall clock, so a loaded CI runner needs more than their nominal length."""
    from PySide6.QtCore import QDeadlineTimer
    deadline = QDeadlineTimer(timeout_ms)
    while not condition():
        if deadline.hasExpired():
            return False
        QTest.qWait(20)
    return True


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
    assert wait_until(window.playback_settings.isVisible)  # the sheet slides in
    QTest.qWait(500)
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
    assert not window.tracks_btn.isChecked()
    assert wait_until(lambda: not window.track_panel.isVisible())  # the card fades out first


def test_status_messages_surface_in_the_subheading(window):
    window.statusBar().showMessage("seeking to 12.0s")
    assert window.subheading.text() == "seeking to 12.0s"
    window.statusBar().clearMessage()
    assert window.subheading.text() == "Not connected"
    assert window.statusBar().isHidden()


def test_accent_for_hue_holds_one_brightness_across_hues():
    from desktop_client.theme import _luminance
    values = [_luminance(theme.accent_for_hue(hue / 12)) for hue in range(12)]
    assert max(values) - min(values) < 0.02
    assert abs(values[0] - 0.33) < 0.02


def test_accent_from_frame_follows_the_dominant_colour_and_ignores_grey():
    QApplication.instance() or QApplication([])
    frame = QImage(16, 9, QImage.Format_RGB32)
    frame.fill(QColor("#101010"))            # letterbox-dark background
    for x in range(10):
        for y in range(6):
            frame.setPixelColor(x, y, QColor("#1e6fe0"))  # a blue subject
    blue = theme.accent_from_frame(frame)
    assert abs(blue.hslHueF() - QColor("#1e6fe0").hslHueF()) < 0.04
    frame.fill(QColor("#f4f4f4"))
    neutral = theme.accent_from_frame(frame)
    assert neutral.hslSaturationF() < 0.05 and neutral.lightnessF() > 0.9


def test_accent_choice_persists_and_restyles(window):
    app = QApplication.instance()
    window.show()
    assert window.settings.accent == "auto"
    window.accent_picker.picked.emit("#4fd68f")
    assert window.settings.accent == "#4fd68f"
    QTest.qWait(theme.ACCENT_FADE + 200)  # the colour glides to its target
    assert theme.accent_source() == QColor("#4fd68f")
    assert app.palette().color(QPalette.Highlight) == QColor(theme.current().accent)
    assert window.connect_btn.palette().color(QPalette.Highlight) == QColor(theme.current().accent)
    window.accent_picker.picked.emit("auto")
    QTest.qWait(theme.ACCENT_FADE + 200)
    assert theme.accent_source() == QColor(theme.DEFAULT_ACCENT)  # nothing is playing
    assert window.accent_hint.text() == "Follows the video that is playing"


def test_wheel_over_a_settings_dropdown_scrolls_the_sheet_instead(window):
    window.resize(900, 500)
    window.show()
    window.playback_settings.show()
    QApplication.instance().processEvents()
    combo = window.fit_combo
    before = combo.currentIndex()
    from PySide6.QtWidgets import QScrollArea
    bar = window.playback_settings.findChild(QScrollArea).verticalScrollBar()
    assert bar.maximum() > 0
    centre = QPointF(combo.rect().center())
    wheel = QWheelEvent(centre, QPointF(combo.mapToGlobal(centre.toPoint())), QPoint(0, 0), QPoint(0, -240),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(combo, wheel)
    assert combo.currentIndex() == before
    assert bar.value() > 0


def test_stop_button_sits_by_connect_and_shows_only_while_usable(window):
    window.show()
    assert window.stop_btn.parentWidget() is window.connect_btn.parentWidget()
    assert not window.stop_btn.isVisible()
    window.stop_btn.setEnabled(True)
    assert window.stop_btn.isVisible()
    window.stop_btn.setEnabled(False)
    assert not window.stop_btn.isVisible()


def test_busy_ring_covers_first_frame_seek_and_rebuffer(window):
    window.show()
    window._session_source = "server_file"
    window.idle_hint.hide()
    window._awaiting_first_frame = True
    window._update_loading()
    assert window.video_overlay.busy
    window._on_position(1.0)
    assert not window.video_overlay.busy
    window._arm_pending_seek(60.0)
    assert window.video_overlay.busy
    window._on_position(60.2)
    assert not window.video_overlay.busy
    window._on_rebuffering(True)
    assert window.video_overlay.busy
    window._on_rebuffering(False)
    assert not window.video_overlay.busy
    window._session_source = None


def test_raw_screenshot_reduces_to_a_colour_thumbnail():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import _thumbnail
    width, height, stride = 64, 36, 64 * 4 + 8  # stride wider than the row, as mpv may pad
    row = bytes([200, 100, 50, 0]) * width + bytes(8)  # b, g, r, unused
    image = _thumbnail({"w": width, "h": height, "stride": stride, "format": "bgr0", "data": row * height})
    assert (image.width(), image.height()) == (16, 9)
    assert image.pixelColor(8, 4) == QColor(50, 100, 200)
    assert _thumbnail({"w": width, "h": height, "stride": stride, "format": "yuv420p", "data": b""}) is None


def test_fullscreen_bar_floats_over_the_window_not_the_video(window):
    window.resize(900, 600)
    window.show()
    window.toggle_fullscreen()
    try:
        # A child of the GL video widget was visible to Qt but not composited.
        assert window.controls_panel.parentWidget() is window._root
        window._reveal_controls()
        QTest.qWait(theme.NORMAL + 150)  # slides up
        bar = window.controls_panel.geometry()
        assert window.controls_panel.isVisible()
        assert bar.bottom() == window._root.height() - 1
    finally:
        window.toggle_fullscreen()
    assert window.controls_panel.isVisible()


def test_sidebar_slides_away_and_back_to_its_width(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    width = window.split.sizes()[0]
    assert width >= 220
    window.browser_toggle.setChecked(False)
    assert window._sidebar_slot.isVisible()  # still sliding out
    assert wait_until(lambda: not window._sidebar_slot.isVisible())
    assert not window.sort_combo.isVisible()
    window.browser_toggle.setChecked(True)
    assert wait_until(lambda: window._sidebar_slot.isVisible() and window.split.sizes()[0] == width)
    assert window.browser_container.width() == window._sidebar_slot.width()  # fills its slot again


def test_fullscreen_slides_the_chrome_out_and_restores_it(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    width = window.split.sizes()[0]
    window.toggle_fullscreen()
    assert wait_until(lambda: not window._toolbar_slot.isVisible() and not window._sidebar_slot.isVisible())
    window.toggle_fullscreen()
    assert wait_until(lambda: window._toolbar_slot.isVisible() and window._toolbar_slot.height() == 64
                      and window._sidebar_slot.isVisible() and window.split.sizes()[0] == width)
    assert window.controls_panel.isVisible()


def test_auto_accent_samples_only_at_restarts_under_display_sync(window):
    requests = []
    window.player.request_frame_sample = lambda: requests.append(1)
    window.player.display_sync_active = lambda: True
    window._session_source = "server_file"
    window._accent_sample_due = True
    window._request_accent_sample()
    window._request_accent_sample()          # periodic ticks after the first are skipped
    assert len(requests) == 1
    window._arm_pending_seek(30.0)
    window._request_accent_sample()          # not while the seek is still landing
    assert len(requests) == 1
    window._on_position(30.1)
    window._request_accent_sample()
    assert len(requests) == 2
    window.player.display_sync_active = lambda: False
    window._request_accent_sample()          # audio sync: every tick samples
    window._request_accent_sample()
    assert len(requests) == 4
    window._session_source = None


def test_display_pacing_is_opt_in_and_follows_fullscreen_in_windowed_mode(window):
    calls = []
    window.player.set_display_rate_reporting = calls.append
    assert window.settings.display_sync == "off"
    assert window.display_sync_switch.current() == "off"
    window.display_sync_switch.selected.emit("windowed")
    assert window.settings.display_sync == "windowed"
    assert calls[-1] is True
    window.show()
    window.toggle_fullscreen()
    assert calls[-1] is True                      # still paced by the display while the chrome slides
    assert wait_until(lambda: calls[-1] is False)  # the transition settles: mpv's own timing
    window.toggle_fullscreen()
    assert calls[-1] is True                      # fluid again before the exit motion
    window.display_sync_switch.selected.emit("always")
    window.toggle_fullscreen()
    QTest.qWait(1200)                             # past the settle point (synchronous test, outside coroutine context)
    assert calls[-1] is True                      # always: fullscreen too
    window.toggle_fullscreen()
    window.display_sync_switch.selected.emit("off")
    assert calls[-1] is False


def test_display_pacing_setting_migrates_the_old_switch(window):
    window.settings._qs.setValue("playback/display_sync", True)
    assert window.settings.display_sync == "windowed"
    window.settings._qs.setValue("playback/display_sync", False)
    assert window.settings.display_sync == "off"
    window.settings.display_sync = "bogus"
    assert window.settings.display_sync == "off"


def test_video_timing_hint_explains_switch_and_mode_together(window):
    sync = window.mpv_controls["video-sync"]
    assert sync.itemData(sync.findText("Audio (default)")) == "audio"
    assert sync.findData("display-resample") >= 0 and sync.findData("display-vdrop") >= 0
    values = dict(window._next_defaults)
    window._refresh_mpv_controls({**values, "video-sync": "audio"})
    assert "paced by audio" in window.display_sync_hint.text()
    window._refresh_mpv_controls({**values, "video-sync": "display-resample"})
    assert sync.currentData() == "display-resample"
    assert "set but inactive" in window.display_sync_hint.text()
    window.display_sync_switch.selected.emit("windowed")
    assert "while the window is not fullscreen" in window.display_sync_hint.text()
    window.display_sync_switch.selected.emit("always")
    assert "fullscreen included" in window.display_sync_hint.text()
    window._refresh_mpv_controls({**values, "video-sync": "audio"})
    assert "No effect yet" in window.display_sync_hint.text()


def test_display_rate_is_reported_only_when_enabled():
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client.mpv_view import MpvPlayerView
    sent = {}
    player = SimpleNamespace(
        options=SimpleNamespace(headless=False), _display_rate=0.0, _report_display=False,
        screen=lambda: SimpleNamespace(refreshRate=lambda: 120.0), mpv=sent,
    )
    player._report_display_rate = lambda: MpvPlayerView._report_display_rate(player)
    MpvPlayerView._report_display_rate(player)
    assert sent == {}                                   # off: mpv is told nothing
    MpvPlayerView.set_display_rate_reporting(player, True)
    assert sent == {"display-fps-override": 120.0}
    MpvPlayerView.set_display_rate_reporting(player, False)
    assert sent == {"display-fps-override": 0.0}        # back to unknown
