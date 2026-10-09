"""Theme and custom-widget behavior the window layout relies on."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPalette, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

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
        headless=True, settings_scope=SCOPE, log_root=tmp_path / "logs",
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


def test_auto_accent_samples_only_at_open_seek_and_pause(window):
    # Each raw screenshot holds mpv's frame delivery up; sampled every 2 s,
    # 13-14% of fullscreen frames landed a refresh or more off schedule.
    requests = []
    window.player.request_frame_sample = lambda: requests.append(1)
    window._session_source = "server_file"
    window._accent_sample_due = True
    window._awaiting_first_frame = True
    window._request_accent_sample()          # not before the first frame
    assert requests == []
    window._awaiting_first_frame = False
    window._request_accent_sample()
    window._request_accent_sample()          # periodic ticks after the first are skipped
    assert len(requests) == 1
    window._arm_pending_seek(30.0)
    window._request_accent_sample()          # not while the seek is still landing
    assert len(requests) == 1
    window._on_position(30.1)
    window._request_accent_sample()
    window._request_accent_sample()
    assert len(requests) == 2
    window._session_source = None


def test_display_pacing_is_opt_in_and_follows_fullscreen(window):
    calls = []
    window.player.set_display_rate_reporting = calls.append
    assert window.settings.display_sync is False
    assert not window.display_sync_check.isChecked()
    window.display_sync_check.setChecked(True)
    assert window.settings.display_sync is True
    assert calls[-1] is True
    window.show()
    window.toggle_fullscreen()
    assert calls[-1] is True                      # still paced by the display while the chrome slides
    assert wait_until(lambda: calls[-1] is False)  # settled in fullscreen: mpv's own timing
    window.toggle_fullscreen()
    assert calls[-1] is True                      # paced by the display again before the exit motion
    window.display_sync_check.setChecked(False)
    assert calls[-1] is False


def test_display_pacing_setting_reads_earlier_mode_names(window):
    for stored, expected in ((True, True), ("windowed", True), ("always", True), (False, False), ("off", False), ("bogus", False)):
        window.settings._qs.setValue("playback/display_sync", stored)
        assert window.settings.display_sync is expected, stored


def test_video_timing_hint_explains_switch_and_mode_together(window):
    sync = window.mpv_controls["video-sync"]
    assert sync.itemData(sync.findText("Audio (default)")) == "audio"
    assert sync.findData("display-resample") >= 0 and sync.findData("display-vdrop") >= 0
    assert "paced by audio" in window.display_sync_hint.text()
    window._edit_mpv_default("video-sync", "display-resample")
    assert sync.currentData() == "display-resample"
    assert "set but inactive" in window.display_sync_hint.text()
    window.display_sync_check.setChecked(True)
    assert "while the window is not fullscreen" in window.display_sync_hint.text()
    window._edit_mpv_default("interpolation", "yes")
    assert "Motion interpolation works only while the window is not fullscreen" in window.display_sync_hint.text()
    window._edit_mpv_default("video-sync", "audio")
    assert "No effect yet" in window.display_sync_hint.text()
    assert "Motion interpolation is on but has no effect" in window.display_sync_hint.text()
    window.display_sync_check.setChecked(False)
    window._edit_mpv_default("interpolation", "no")
    assert window.display_sync_hint.text() == "Video is paced by audio, mpv's default."


def test_mpv_settings_outside_the_offered_choices_read_as_defaults(window):
    qs = window.settings._qs
    qs.setValue("mpv/video-sync", "display-tempo")      # not offered
    qs.setValue("mpv/tscale", "mitchell")
    assert window.settings.mpv_defaults["video-sync"] == "audio"
    assert window.settings.mpv_defaults["tscale"] == "mitchell"
    with pytest.raises(ValueError):
        window.settings.set_mpv_default("video-sync", "display-tempo")
    with pytest.raises(KeyError):
        window.settings.set_mpv_default("scale", "ewa_lanczossharp")


def test_display_rate_is_reported_only_when_enabled():
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client.mpv_view import MpvPlayerView
    sent = {}
    player = SimpleNamespace(
        options=SimpleNamespace(headless=False), _display_rate=0.0, _report_display=False,
        screen=lambda: SimpleNamespace(refreshRate=lambda: 120.0), mpv=sent, _presenting=lambda: True,
    )
    player._report_display_rate = lambda: MpvPlayerView._report_display_rate(player)
    MpvPlayerView._report_display_rate(player)
    assert sent == {}                                   # off: mpv is told nothing
    MpvPlayerView.set_display_rate_reporting(player, True)
    assert sent == {"display-fps-override": 120.0}
    MpvPlayerView.set_display_rate_reporting(player, False)
    assert sent == {"display-fps-override": 0.0}        # back to unknown


def test_video_follows_the_chrome_through_a_fullscreen_transition(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    layout = window._player_layout
    bar = window.controls_panel
    height = bar.height()
    window.toggle_fullscreen()
    # Never pinned: pinning made the picture jump to its final size at once.
    assert layout.indexOf(window.player) >= 0
    # The bar left the layout but rides on its stand-in, which the video
    # grows into as it shrinks.
    assert window._controls_dock.isVisible() and window._controls_dock.height() == height
    assert bar.isVisible() and bar.geometry().top() == window._root.height() - height
    sizes = []
    assert wait_until(lambda: sizes.append(window.player.height()) or not window._controls_dock.isVisible())
    assert len(set(sizes)) > 2  # grew step by step
    assert not bar.isVisible()  # hidden in its overlay place until the pointer calls it
    assert window._controls_layout.indexOf(bar) < 0
    window.toggle_fullscreen()
    assert window._controls_dock.isVisible() and bar.isVisible()
    assert bar.geometry().top() >= window._root.height() - 1  # rises from the bottom
    assert wait_until(lambda: not window._controls_dock.isVisible()
                      and window._toolbar_slot.isVisible() and window._sidebar_slot.isVisible())
    assert window._controls_layout.indexOf(bar) == 1  # docked below the video again
    QApplication.instance().processEvents()
    assert bar.geometry().bottom() == window._root.height() - 1
    assert window.player.geometry().topLeft() == QPoint(0, 64)  # back in its place under the top bar


def test_reversing_fullscreen_midway_continues_the_control_bar_from_where_it_is(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    window.toggle_fullscreen()
    QTest.qWait(theme.SLOW // 3)
    midway = window._controls_dock_h
    assert midway is not None
    window.toggle_fullscreen()
    assert window._controls_dock_h == midway  # no jump back to either end
    assert wait_until(lambda: window._controls_layout.indexOf(window.controls_panel) == 1)
    assert window.controls_panel.isVisible() and not window._controls_dock.isVisible()


def test_sidebar_collapses_and_expands_the_same_way(window):
    # Both directions resize the video at each step; the hide slide used to pin
    # it at its final size instead, so the two looked different.
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    layout = window._player_layout
    for visible in (False, True):
        widths = []
        window.browser_toggle.setChecked(visible)
        assert wait_until(lambda: widths.append(window.player.width()) or "sidebar" not in window._slides)
        assert layout.indexOf(window.player) >= 0
        assert len(set(widths)) > 2  # stepped, not jumped
        assert widths == sorted(widths, reverse=visible)


class _FrameSource(QObject):
    frameSwapped = Signal()


def _frame_animation(start=0, end=300, frames=True):
    from desktop_client.widgets import FrameAnimation, emphasized
    QApplication.instance() or QApplication([])
    view, nudge = _FrameSource(), QWidget()
    values, landed = [], []
    animation = FrameAnimation(frames=(lambda: view.frameSwapped) if frames else None, nudge=nudge)
    animation.setDuration(theme.SLOW)
    animation.setEasingCurve(emphasized())
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.valueChanged.connect(values.append)
    animation.finished.connect(lambda: landed.append(values[-1] if values else None))
    return animation, view, nudge, values, landed


def test_frame_animation_steps_once_per_presented_frame_and_lands():
    animation, view, _nudge, values, landed = _frame_animation()
    animation.start()
    assert animation.frame_paced
    steps = len(values)
    view.frameSwapped.emit()
    assert len(values) == steps + 1  # one step per presented frame
    for _ in range(500):
        if landed:
            break
        QTest.qWait(8)
        view.frameSwapped.emit()
    assert values == sorted(values) and len(values) > 10 and landed == [300.0]
    count = len(values)
    view.frameSwapped.emit()  # finished: later frames change nothing
    assert len(values) == count and not animation.frame_paced


def test_frame_animation_lands_when_no_frames_are_presented():
    # An unexposed window presents nothing; the animation must still finish.
    animation, _view, _nudge, values, landed = _frame_animation()
    animation.start()
    assert wait_until(lambda: landed, timeout_ms=theme.SLOW + 1000)
    assert landed == [300.0]


def test_stopped_frame_animation_ignores_later_frames():
    animation, view, _nudge, values, landed = _frame_animation()
    animation.start()
    QTest.qWait(30)
    view.frameSwapped.emit()
    animation.stop()
    applied = list(values)
    view.frameSwapped.emit()
    QTest.qWait(120)  # past the stall interval too
    assert values == applied and not landed


def test_frame_animation_without_frames_is_a_timed_animation():
    # The tray GUI and offscreen runs have no frames to follow.
    animation, _view, _nudge, values, landed = _frame_animation(frames=False)
    animation.start()
    assert not animation.frame_paced
    assert wait_until(lambda: landed, timeout_ms=theme.SLOW + 1000)
    assert values[-1] == 300.0


def test_window_slides_step_on_the_video_frames_when_it_presents_them(window, monkeypatch):
    source = _FrameSource(window)
    window.player.frameSwapped = source.frameSwapped
    monkeypatch.setattr(window, "_frame_paced", lambda: True)
    frames = QTimer(window)
    frames.timeout.connect(source.frameSwapped.emit)
    frames.start(8)
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    width = window.split.sizes()[0]
    window.browser_toggle.setChecked(False)
    assert window._slides["sidebar"].frame_paced
    assert wait_until(lambda: not window._sidebar_slot.isVisible())
    window.browser_toggle.setChecked(True)
    assert window._slides["sidebar"].frame_paced
    assert wait_until(lambda: window._sidebar_slot.isVisible() and window.split.sizes()[0] == width)
    frames.stop()


def test_resize_skips_the_render_qt_would_discard_and_requests_one_that_shows():
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client.mpv_view import MpvPlayerView
    renders = []
    player = SimpleNamespace(
        _ctx=SimpleNamespace(render=lambda **kw: renders.append(kw)), _resizing=True,
        _scaling=False, _picture_fbo=None,
        devicePixelRatioF=lambda: 1.0, defaultFramebufferObject=lambda: 1,
        width=lambda: 64, height=lambda: 36,
    )
    MpvPlayerView.paintGL(player)  # inside QOpenGLWidget's resizeEvent
    assert renders == []
    player._resizing = False
    MpvPlayerView.paintGL(player)  # the repaint that is composited
    assert len(renders) == 1


def test_resized_player_always_schedules_a_repaint():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(headless=True, settings_scope=SCOPE))
    try:
        updates = []
        player.update = lambda *args: updates.append(args)
        player.resize(320, 180)
        player.show()
        player.resize(400, 200)
        QApplication.instance().processEvents()
        # Qt does not always repaint a resized QOpenGLWidget on its own; one
        # composited from its fresh framebuffer would show a blank frame.
        assert updates and not player._resizing
    finally:
        player.close()
        player.mpv.terminate()


def test_reveal_reanchor_continues_from_where_the_panel_is():
    from desktop_client.widgets import Reveal
    QApplication.instance() or QApplication([])
    parent = QWidget()
    parent.resize(400, 300)
    panel = QWidget(parent)
    panel.setGeometry(0, 270, 400, 60)  # 30 of its 60 px showing
    reveal = Reveal(panel, offset=QPoint(0, 60), fade=False, show_ms=100)
    shown_at = QRect(0, 240, 400, 60)
    reveal.reanchor(shown_at)
    assert panel.geometry().topLeft() == QPoint(0, 270)  # not moved
    assert reveal._t == pytest.approx(0.5)  # half revealed
    reveal.show(shown_at)
    assert wait_until(lambda: panel.geometry() == shown_at, timeout_ms=1000)


def test_pointer_calls_the_bar_back_while_it_slides_away(window):
    window.resize(900, 600)
    window.show()
    QApplication.instance().processEvents()
    window.toggle_fullscreen()
    try:
        QTest.qWait(theme.SLOW // 2)
        assert window._controls_dock.isVisible()  # still sliding off
        window._reveal_controls()
        assert wait_until(lambda: not window._controls_dock.isVisible())
        QTest.qWait(theme.NORMAL + 50)
        bar = window.controls_panel
        assert bar.isVisible() and bar.geometry().bottom() == window._root.height() - 1
    finally:
        window.toggle_fullscreen()
    assert wait_until(lambda: window._controls_layout.indexOf(window.controls_panel) == 1)


def test_fit_letterboxes_and_pillarboxes():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import _fit
    assert _fit(1440, 900, 16 / 9) == (0.0, 45.0, 1440.0, 810.0)
    assert _fit(2000, 900, 16 / 9) == pytest.approx((200.0, 0.0, 1600.0, 900.0))


def test_resizes_scale_the_picture_only_when_mpv_simply_letterboxes_it():
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client.mpv_view import MpvPlayerView
    plain = {"dwidth": 1920, "dheight": 1080, "panscan": 0.0, "video-zoom": 0.0, "keepaspect": True,
             "video-unscaled": False, "video-rotate": 0}
    player = SimpleNamespace(_ctx=object(), _scaling_broken=False, _observed=dict(plain))
    assert MpvPlayerView._picture_aspect(player) == pytest.approx(16 / 9)
    for name, value in (("panscan", 1.0), ("video-zoom", 0.5), ("video-pan-x", 0.1), ("video-align-y", 1.0),
                        ("video-unscaled", True), ("video-rotate", 90), ("keepaspect", False), ("dwidth", None)):
        player._observed = {**plain, name: value}
        assert MpvPlayerView._picture_aspect(player) is None, name  # rendered at every size instead
    player._observed = dict(plain)
    player._scaling_broken = True
    assert MpvPlayerView._picture_aspect(player) is None


class _FakeFbo:
    handles = iter(range(100, 200))

    def __init__(self, size):
        self._size = size
        self._handle = next(self.handles)

    def width(self):
        return self._size.width()

    def height(self):
        return self._size.height()

    def handle(self):
        return self._handle


class _GlRecorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


def test_scaled_paint_maps_the_picture_onto_where_mpv_would_put_it(monkeypatch):
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client import mpv_view
    from desktop_client.mpv_view import MpvPlayerView
    gl = _GlRecorder()
    monkeypatch.setattr(mpv_view, "QOpenGLContext",
                        SimpleNamespace(currentContext=lambda: SimpleNamespace(extraFunctions=lambda: gl)))
    monkeypatch.setattr(mpv_view, "QOpenGLFramebufferObject", _FakeFbo)
    renders, frames = [], [False]
    player = SimpleNamespace(
        _ctx=SimpleNamespace(update=lambda: frames[0], render=lambda **kw: renders.append(kw["opengl_fbo"])),
        _picture_fbo=None, _picture_due=True, _picture_aspect=lambda: 16 / 9,
        defaultFramebufferObject=lambda: 7,
    )
    # The resize starts: the current frame goes into our own framebuffer once.
    assert MpvPlayerView._paint_scaled(player, 2254, 1472)
    first = player._picture_fbo
    assert renders == [{"fbo": first.handle(), "w": 2254, "h": 1472}]
    # Grown to fullscreen with no new video frame: no render, only a blit,
    # scaled so the picture lands on its fullscreen letterbox (0, 90, 2880x1620).
    gl.calls.clear()
    assert MpvPlayerView._paint_scaled(player, 2880, 1800)
    assert len(renders) == 1
    blit = next(args for name, args in gl.calls if name == "glBlitFramebuffer")
    src = blit[:4]
    dst = blit[4:8]
    assert src == (0, 0, 2254, 1472)
    scale = 2880 / 2254
    picture_bottom = dst[1] + (1472 - 2254 * 9 / 16) / 2 * scale
    assert dst[0] == 0 and dst[2] == 2880
    assert picture_bottom == pytest.approx(90, abs=1)
    assert ("glBindFramebuffer", (mpv_view._GL_DRAW_FRAMEBUFFER, 7)) in gl.calls
    # A new video frame once the view has outgrown the picture: re-rendered at this size.
    frames[0] = True
    assert MpvPlayerView._paint_scaled(player, 2880, 1800)
    assert player._picture_fbo is not first
    assert renders[-1] == {"fbo": player._picture_fbo.handle(), "w": 2880, "h": 1800}


def test_a_failing_scaled_paint_falls_back_instead_of_escaping_the_qt_virtual():
    pytest.importorskip("mpv")
    from types import SimpleNamespace
    from desktop_client.mpv_view import MpvPlayerView
    renders, logged = [], []

    def broken(_width, _height):
        raise RuntimeError("no blit")

    player = SimpleNamespace(
        _ctx=SimpleNamespace(render=lambda **kw: renders.append(kw["opengl_fbo"])),
        _resizing=False, _scaling=True, _scaling_broken=False, _picture_fbo=None,
        _paint_scaled=broken, devicePixelRatioF=lambda: 2.0, width=lambda: 100, height=lambda: 50,
        defaultFramebufferObject=lambda: 7, log_message=SimpleNamespace(emit=lambda *args: logged.append(args)),
    )
    MpvPlayerView.paintGL(player)  # an exception here would be a native crash
    assert renders == [{"fbo": 7, "w": 200, "h": 100}]
    assert player._scaling_broken and not player._scaling and logged


def test_resizing_settles_into_one_sharp_render():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(headless=True, settings_scope=SCOPE))
    try:
        player._picture_aspect = lambda: 16 / 9
        updates = []
        player.update = lambda *args: updates.append(args)
        player.resize(320, 180)
        player.show()
        player.resize(400, 200)
        assert player._scaling and player._picture_due
        updates.clear()
        assert wait_until(lambda: not player._scaling, timeout_ms=1000)
        assert updates  # the sharp render at the settled size
    finally:
        player.close()
        player.mpv.terminate()


def test_overlays_step_on_the_video_frames_when_it_presents_them(window, monkeypatch):
    source = _FrameSource(window)
    window.player.frameSwapped = source.frameSwapped
    monkeypatch.setattr(window, "_frame_paced", lambda: True)
    frames = QTimer(window)
    frames.timeout.connect(source.frameSwapped.emit)
    frames.start(8)
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    window.tracks_btn.setChecked(True)
    assert window._track_reveal._animation.frame_paced
    assert wait_until(lambda: window._track_reveal._t == 1.0)
    window.tracks_btn.setChecked(False)
    assert wait_until(lambda: not window.track_panel.isVisible())
    frames.stop()


def test_seek_bar_presses_anywhere_on_its_height_and_drags_by_its_own_mapping(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    slider = window.seek_slider
    slider.setEnabled(True)
    assert slider.height() == 26  # 30% taller than the other sliders
    events = []
    slider.sliderPressed.connect(lambda: events.append("pressed"))
    slider.sliderReleased.connect(lambda: events.append("released"))
    slider.sliderMoved.connect(lambda value: events.append(value))
    from PySide6.QtWidgets import QStyle
    width = slider.width()
    def under(x):
        return QStyle.sliderValueFromPosition(slider.minimum(), slider.maximum(), x, width)
    QTest.mousePress(slider, Qt.LeftButton, Qt.NoModifier, QPoint(width // 4, 1))  # near the top edge
    assert events[:2] == ["pressed", under(width // 4)]
    QTest.mouseMove(slider, QPoint(width // 10, slider.height() - 2))
    assert slider.sliderPosition() == under(width // 10)  # under the pointer, dragging left
    QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier, QPoint(width // 10, slider.height() - 2))
    assert events[-1] == "released" and not slider.isSliderDown()


def test_scrubbing_does_not_resize_the_seek_bar(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    window._duration_s = 7200.0
    window._position_s = 3600.0
    width = window.seek_slider.width()
    for value in (0, 499, 500, 1000):
        window._scrub_preview(value)  # "60:00 (-3600.0s)" and the like
        QApplication.instance().processEvents()
        assert window.seek_slider.width() == width
    window._cancel_scrub()


def test_seek_tip_sits_beside_the_pointer_and_lets_the_mouse_through(window):
    from desktop_client.widgets import SliderTip
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    slider = window.seek_slider
    slider.setEnabled(True)
    window._duration_s = 600.0
    QTest.mouseMove(slider, QPoint(slider.width() // 2, slider.height() // 2))
    tip = slider._tip
    assert isinstance(tip, SliderTip) and tip.isVisible()
    assert tip.testAttribute(Qt.WA_TransparentForMouseEvents)
    pointer = slider.mapTo(window, QPoint(slider.width() // 2, 0)).x()
    assert tip.geometry().left() > pointer and tip._caret_left  # to the right, caret pointing back
    QTest.mouseMove(slider, QPoint(slider.width() - 1, slider.height() // 2))
    end = slider.mapTo(window, QPoint(slider.width() - 1, 0)).x()
    assert not tip._caret_left and tip.geometry().right() < end  # flipped: not over the time readout
    window._duration_s = None


def test_now_playing_corner_opens_its_full_text_in_a_card(window):
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    corner = window._now_playing
    QTest.mouseClick(corner, Qt.LeftButton)
    assert not window._info_reveal.shown  # nothing playing: nothing to show
    long_name = "[SubsPlease] A Very Long Episode Title That Will Not Fit In The Corner - 01 (1080p) [ABCDEF12].mkv"
    window._show_now_playing(long_name, "model · quality · 2880×1620",
                             [("File", "data4/Unwatched/" + long_name), ("Upscale model", "2x_Model_fp16")])
    QTest.mouseClick(corner, Qt.LeftButton)
    assert window._info_reveal.shown
    assert window.info_title.text() == long_name
    assert window.info_rows.rowCount() == 2
    assert window.now_title._highlighted
    card = window._info_geometry()
    assert card.left() == window._CONTROL_MARGINS[0] and card.bottom() < window._controls_top()
    QTest.keyClick(window, Qt.Key_Escape)
    assert not window._info_reveal.shown
    QTest.mouseClick(corner, Qt.LeftButton)
    window.tracks_btn.setChecked(True)  # one card at a time
    assert not window._info_reveal.shown
    window.tracks_btn.setChecked(False)
    window._show_now_playing(None)
    assert not window._info_reveal.shown and not window.info_panel.isVisible()


def test_settings_sheet_close_button_slides_it_out(window):
    from desktop_client.widgets import IconButton
    window.resize(1100, 600)
    window.show()
    QApplication.instance().processEvents()
    window.playback_settings_toggle.setChecked(True)
    assert wait_until(lambda: window._settings_reveal._t == 1.0)
    close = next(b for b in window.playback_settings.findChildren(IconButton) if b.toolTip() == "Close")
    close.click()
    assert window.playback_settings.isVisible() and not window._settings_reveal.shown  # sliding out
    assert wait_until(lambda: not window.playback_settings.isVisible())
    assert not window.playback_settings_toggle.isChecked()


def test_browser_shows_watch_progress_beside_the_icon():
    from desktop_client.history import HistoryEntry
    assert HistoryEntry("k", 600.0, 1200.0).progress == "50%"
    assert HistoryEntry("k", 1199.0, 1200.0).progress == "99%"
    assert HistoryEntry("k", 10.0, 1200.0, watched=True).progress == "\u2713"
    assert HistoryEntry("k", 10.0, None).progress == ""
