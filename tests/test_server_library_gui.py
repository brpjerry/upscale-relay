"""Offscreen desktop-client coverage for the server library UI."""

from __future__ import annotations

import asyncio
import os
import socket
from fractions import Fraction
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PySide6 = pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtGui import QColor, QPalette

import desktop_client.main_window as main_window
from desktop_client.options import DesktopOptions
from desktop_client.mpv_view import _LoopbackStream


class FakePlayer(QWidget):
    stats_changed = Signal(str)
    position_changed = Signal(float)
    track_list_changed = Signal(list, object)
    audio_track_list_changed = Signal(list, object)
    volume_changed = Signal(int, bool)
    rebuffering = Signal(bool)
    pause_requested = Signal()
    seek_requested = Signal(float)
    finished = Signal()
    failed = Signal(str)
    fullscreen_toggled = Signal()
    mouse_moved = Signal(int, int)

    def __init__(self, options=None):
        super().__init__()
        self.client = None
        self.started = None
        self.font_dir = None

    def start(self, session, queue, time_base, source_path=None, avg_rate=None,
              source_has_audio=True):
        self.started = (session, queue, time_base, source_path, avg_rate)
        self.source_has_audio = source_has_audio

    def stop(self):
        pass

    def set_panscan(self, value):
        pass

    def set_paused(self, value):
        self.paused = value

    def set_subtitle_fonts_dir(self, path):
        self.font_dir = path

    def set_sub_delay(self, value):
        pass

    def set_audio_delay(self, value):
        pass

    def select_subtitle(self, sid):
        pass

    def select_audio(self, aid):
        pass

    async def play_local(self, path, position_s=0.0, *, paused=False):
        self.local_playback = (path, position_s, paused)

    def seek_local(self, target_s):
        self.local_seek = target_s

    def set_volume(self, percent):
        self.volume = percent

    def set_muted(self, muted):
        self.muted = muted


class FakeLibraryClient:
    host = "media-server"
    port = 8590
    session = None
    track = None

    def __init__(self):
        self.fetches = []

    async def fetch_library_page(self, path="", *, cursor=None, limit=100):
        self.fetches.append((path, cursor, limit))
        children = (
            [{"type": "directory", "name": "Shows", "path": "Shows", "children": []}]
            if path == "" else
            [{"type": "file", "name": "Episode.mkv", "path": "Shows/Episode.mkv"}]
        )
        return {
            "tree": {"type": "directory", "name": path or "Library", "path": path,
                     "children": children},
            "next_cursor": None,
        }


class FakeSessionClient(FakeLibraryClient):
    def __init__(self):
        super().__init__()
        self.session = None
        self.track = None
        self.opened_config = None
        self.queue = asyncio.Queue()
        self.attachment_cache_root = None

    async def open_session(self, config):
        self.opened_config = config
        self.session = SimpleNamespace(
            downlink_codec="hevc", downlink_width=1920, downlink_height=1080,
            downlink_container="matroska", time_base=Fraction(1, 1000),
            duration_s=120.0, avg_rate=Fraction(24, 1),
            chapters=getattr(self, "chapters", None),
            aux_tracks=config.aux_tracks,
            aux_attachments=config.aux_attachments,
        )
        return self.session

    async def prepare_attachments(self, cache_root):
        self.attachment_cache_root = cache_root
        return "C:/verified-fonts" if self.opened_config.aux_attachments == "cached" else None

    async def attach_media(self):
        pass

    async def start_uplink(self):
        pass

    async def play(self):
        pass

    def downlink_queue(self):
        return self.queue

    def media_url(self, path):
        return f"http://media-server:8590/media/{path}"


class FakePagedLibraryClient(FakeLibraryClient):
    async def fetch_library_page(self, path="", *, cursor=None, limit=100):
        self.fetches.append((path, cursor, limit))
        name = "Episode 1.mkv" if cursor is None else "Episode 2.mkv"
        return {
            "tree": {"type": "directory", "name": "Library", "path": "", "children": [
                {"type": "file", "name": name, "path": name}
            ]},
            "next_cursor": "1" if cursor is None else None,
        }


@pytest.fixture()
def window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "PlayerView", FakePlayer)
    result = main_window.MainWindow(options=DesktopOptions(
        headless=True, settings_scope="test-server-library-gui"
    ))
    yield result
    result.client = None
    result.close()
    app.processEvents()


def test_server_tab_appears_populates_and_disappears(window):
    async def scenario():
        client = FakeLibraryClient()
        await window._adopt_connected_client(client, {
            "server_name": "test", "models": [{"name": "passthrough"}], "library": True,
        })
        assert window.browser_panel.count() == 2
        assert not window.browser_panel.tabBar().isHidden()
        assert window.server_model.rowCount() == 1
        folder = window.server_model.item(0)
        assert folder.child(0).data(Qt.UserRole + 1) == "placeholder"
        await window.on_server_directory_expanded(folder.index())
        episode = folder.child(0)
        assert episode.text() == "Episode.mkv"
        assert episode.data(Qt.UserRole) == "Shows/Episode.mkv"
        assert episode.data(Qt.UserRole + 1) == "file"
        assert client.fetches == [("", None, 100), ("Shows", None, 100)]

        window._remove_server_tab()
        assert window.browser_panel.count() == 1
        assert window.browser_panel.tabBar().isHidden()

    asyncio.run(scenario())


def test_server_load_more_appends_without_reloading(window):
    async def scenario():
        client = FakePagedLibraryClient()
        await window._adopt_connected_client(client, {
            "server_name": "test", "models": [{"name": "passthrough"}], "library": True,
        })
        assert [window.server_model.item(row).text() for row in range(2)] == [
            "Episode 1.mkv", "Load more…",
        ]
        await window.on_server_file_activated(window.server_model.item(1).index())
        assert [window.server_model.item(row).text() for row in range(2)] == [
            "Episode 1.mkv", "Episode 2.mkv",
        ]
        assert client.fetches == [("", None, 100), ("", "1", 100)]

    asyncio.run(scenario())


def test_server_session_uses_http_original_and_server_metadata(window):
    async def scenario():
        client = FakeSessionClient()
        window.client = client
        await window._start_session("Shows/Episode.mkv", source="server_file")

        assert client.opened_config.source == "server_file"
        assert window._session_source == "server_file"
        assert window._session_time_base == Fraction(1, 1000)
        assert window._duration_s == 120.0
        assert window.fallback_btn.isHidden()
        assert window.player.started[3] == (
            "http://media-server:8590/media/Shows/Episode.mkv"
        )
        assert window.player.started[4] == Fraction(24, 1)

    asyncio.run(scenario())


def test_server_session_opts_into_muxed_tracks_and_skips_http_original(window):
    async def scenario():
        client = FakeSessionClient()
        window.client = client
        window._server_caps = {"muxed_aux_tracks": True}
        await window._start_session("Shows/Episode.mkv", source="server_file")

        assert client.opened_config.aux_tracks == "muxed"
        assert window.player.started[3] is None

    asyncio.run(scenario())


def test_server_session_negotiates_cached_fonts_before_player_load(window):
    async def scenario():
        client = FakeSessionClient()
        window.client = client
        window._server_caps = {"muxed_aux_tracks": True, "attachment_cache": 1}
        await window._start_session("Shows/Episode.mkv", source="server_file")

        assert client.opened_config.aux_attachments == "cached"
        assert client.attachment_cache_root.name == "attachments"
        assert window.player.font_dir == "C:/verified-fonts"
        assert window.player.started is not None

    asyncio.run(scenario())


def test_session_chapters_populate_and_clear_controls(window):
    async def scenario():
        client = FakeSessionClient()
        client.chapters = [
            {"start_s": 0.0, "end_s": 40.0, "title": "Opening"},
            {"start_s": 40.0, "end_s": 80.0, "title": None},
            {"start_s": 80.0, "end_s": 120.0, "title": "Ending"},
        ]
        window.client = client
        await window._start_session("Shows/Episode.mkv", source="server_file")

        assert not window.chapter_combo.isHidden()
        assert not window.chapter_prev_btn.isHidden()
        assert window.chapter_combo.count() == 3
        assert window.chapter_combo.itemText(0) == "01  Opening  (00:00)"
        assert window.chapter_combo.itemText(1) == "02  Chapter 2  (00:40)"
        assert window.chapter_combo.itemData(2) == 80.0
        assert window.seek_slider._chapter_fractions == [40.0 / 120.0, 80.0 / 120.0]

        window._on_position(45.0)
        assert window.chapter_combo.currentIndex() == 1

        window.client = None  # the fake has no teardown; skip the reconnect path
        await window._teardown_session()
        assert window.chapter_combo.isHidden()
        assert window.chapter_combo.count() == 0
        assert window.seek_slider._chapter_fractions == []

    asyncio.run(scenario())


def test_media_metadata_cannot_expand_transport_minimum_width(window):
    """Track titles and telemetry must not resize the splitter on load."""
    app = QApplication.instance()
    window._duration_s = 120.0
    window._set_chapters([main_window.Chapter(0.0, "A")])
    window._on_audio_tracks([(1, "A")], 1)
    window._on_tracks([(1, "S")], 1)
    window.player_status.setText("ok")
    window.show()
    app.processEvents()
    baseline = window.controls_panel.minimumSizeHint().width()

    long_title = "A very long muxed track title " * 20
    window._set_chapters([main_window.Chapter(0.0, long_title)])
    window._on_audio_tracks([(1, long_title)], 1)
    window._on_tracks([(1, long_title)], 1)
    window.player_status.setText("cache / decode / drift telemetry " * 30)
    app.processEvents()

    assert window.controls_panel.minimumSizeHint().width() == baseline


def test_splitter_defers_expensive_video_resize_until_release(window):
    assert not window.split.opaqueResize()


def test_open_progress_indicator_toggles(window):
    assert window.open_progress.isHidden()
    window._on_open_progress({"message": "Preparing anime — TensorRT engine", "elapsed_s": 12.0})
    assert not window.open_progress.isHidden()
    assert window.statusBar().currentMessage() == "Preparing anime — TensorRT engine (12 s)"
    window._set_opening(False)
    assert window.open_progress.isHidden()


def test_keyboard_pause_uses_the_toolbar_and_server_state(window):
    class PauseClient(FakeLibraryClient):
        session = object()
        pauses = 0
        plays = 0

        async def pause(self):
            self.pauses += 1

        async def play(self):
            self.plays += 1

    async def scenario():
        client = PauseClient()
        window.client = client
        window.player.pause_requested.emit()
        await asyncio.sleep(0)
        assert window._paused and window.player.paused
        assert client.pauses == 1
        assert window.play_btn.toolTip() == "Play (Space)"
        await window.on_play_pause()
        assert not window._paused and not window.player.paused
        assert client.plays == 1

    asyncio.run(scenario())


def test_keyboard_pause_is_ignored_without_an_active_session(window):
    async def scenario():
        window.client = FakeLibraryClient()  # connected, but no file open
        window.player.pause_requested.emit()
        await asyncio.sleep(0)
        assert not window._paused
        assert not hasattr(window.player, "paused")

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["open", "attachments", "media", "uplink"])
def test_startup_failure_releases_allocated_session(window, monkeypatch, stage):
    class FailingClient(FakeSessionClient):
        has_server_session = False
        released = False

        async def open_session(self, config):
            self.has_server_session = True
            if stage == "open":
                raise OSError("open failed after allocation")
            return await super().open_session(config)

        async def prepare_attachments(self, root):
            if stage == "attachments":
                raise OSError("attachment transfer failed")

        async def attach_media(self):
            if stage == "media":
                raise OSError("media connection failed")

        async def start_uplink(self):
            if stage == "uplink":
                raise OSError("source pump failed")

        async def teardown(self):
            self.released = True

    class Reconnected(FakeLibraryClient):
        def __init__(self, host, port):
            super().__init__()

        async def connect(self):
            return {"server_name": "test", "models": [{"name": "passthrough"}]}

    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    monkeypatch.setattr(main_window, "RelayClient", Reconnected)

    async def scenario():
        client = FailingClient()
        window.client = client
        await window._start_session("Shows/Episode.mkv", source="server_file")
        assert client.released
        assert isinstance(window.client, Reconnected)
        assert not window.stop_btn.isEnabled()
        assert window._session_path is None
        assert window.open_progress.isHidden()
        assert errors and errors[0][0] == "Session failed"

    asyncio.run(scenario())


def test_startup_cleanup_does_not_replace_unconfirmed_server(window, monkeypatch):
    class FailingClient(FakeSessionClient):
        async def prepare_attachments(self, root):
            raise OSError("attachment transfer failed")

        async def teardown(self):
            raise main_window.TeardownNotConfirmedError("native cleanup unconfirmed")

    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))

    async def scenario():
        window.client = FailingClient()
        await window._start_session("Shows/Episode.mkv", source="server_file")
        assert window.client is None
        assert window.conn_label.text() == "server teardown unconfirmed"
        assert errors[-1][0] == "Server cleanup not confirmed"

    asyncio.run(scenario())


def test_loopback_stream_delivers_queued_bytes_and_reports_stats():
    stream = _LoopbackStream()
    host_port = stream.uri.removeprefix("tcp://").split(":")
    receiver = socket.create_connection((host_port[0], int(host_port[1])))
    receiver.settimeout(2)
    stream.feed(b"abc")
    stream.feed(b"defgh")
    assert stream.stats()["chunks"] == 2
    assert stream.stats()["queued_bytes"] == 8
    stream.finish()
    received = bytearray()
    while data := receiver.recv(1024):
        received.extend(data)
    receiver.close()
    assert received == b"abcdefgh"
    assert stream.stats()["chunks"] == 0
    assert stream.stats()["queued_bytes"] == 0
    assert stream.stats()["total_read_bytes"] == 8


def test_loopback_stream_abort_unblocks_listener():
    stream = _LoopbackStream()
    stream.feed(b"discard me")
    stream.abort()
    stream._thread.join(timeout=2)
    assert not stream._thread.is_alive()
    assert stream.stats()["chunks"] == 0
    assert stream.stats()["queued_bytes"] == 0


def test_loopback_stream_abort_unblocks_a_stalled_reader():
    """Stop must finish the native sender even if mpv no longer reads."""
    stream = _LoopbackStream()
    host, port = stream.uri.removeprefix("tcp://").split(":")
    receiver = socket.create_connection((host, int(port)))
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    try:
        stream.feed(b"x" * (16 * 1024 * 1024))
        # Wait until the sender has dequeued the large chunk. With the peer's
        # receive window closed it cannot finish writing this payload.
        import time

        deadline = time.monotonic() + 2
        while stream.stats()["chunks"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stream.stats()["chunks"] == 0
        stream.abort()
        assert not stream._thread.is_alive()
        assert stream.stats()["queued_bytes"] == 0
    finally:
        receiver.close()
        stream.abort()


def test_local_fallback_keeps_transport_timeline_and_chapters(window):
    class FallbackClient(FakeSessionClient):
        released = False

        async def teardown(self):
            self.released = True

    async def scenario():
        client = FallbackClient()
        window.client = client
        window._session_source = "uplink"
        window._session_path = "/tmp/original.mkv"
        window._duration_s = 120.0
        window._position_s = 20.0
        window._paused = True
        window._set_chapters([
            main_window.Chapter(0.0, "Opening"),
            main_window.Chapter(60.0, "Second half"),
        ])
        window.play_btn.setEnabled(True)
        window.seek_slider.setEnabled(True)
        await window.on_fallback()
        assert client.released
        assert window.client is None
        assert window._session_source == "local"
        assert window.player.local_playback == ("/tmp/original.mkv", 20.0, True)

        await window.on_play_pause()
        assert not window.player.paused
        await window.on_seek_relative(5.0)
        assert window.player.local_seek == 25.0
        await window.on_chapter_step(1)
        assert window.player.local_seek == 60.0
        window.player.position_changed.emit(60.5)
        assert window._position_s == 60.5
        assert window.pos_label.text() == "01:00 / 02:00"

        await window.on_stop()
        assert window._session_source is None
        assert not window.play_btn.isEnabled()

    asyncio.run(scenario())


@pytest.mark.parametrize("background,foreground", [
    ("#f0f0f0", "#101010"), ("#202020", "#f0f0f0"),
])
def test_fullscreen_labels_follow_a_readable_theme(window, background, foreground):
    palette = QPalette(window.palette())
    palette.setColor(QPalette.Window, QColor(background))
    palette.setColor(QPalette.WindowText, QColor(foreground))
    window.setPalette(palette)
    window._enter_overlay_controls()
    text = window.pos_label.palette().color(QPalette.WindowText)
    surface = window.controls_panel.palette().color(QPalette.Window)
    assert abs(text.lightness() - surface.lightness()) >= 190
    assert surface.alpha() >= 230
    window._exit_overlay_controls()
    assert window.pos_label.palette().color(QPalette.WindowText) == QColor(foreground)


def test_fullscreen_cursor_hides_on_idle_and_restores_on_motion_and_exit(window, monkeypatch):
    monkeypatch.setattr(window, "_pointer_over_controls", lambda: False)
    window.toggle_fullscreen()
    assert window._cursor_timer.isActive()
    window._auto_hide_cursor()
    assert window.player.cursor().shape() == Qt.BlankCursor
    window._on_player_mouse_moved(100, 100)
    assert window.player.cursor().shape() != Qt.BlankCursor
    assert window._cursor_timer.isActive()
    window._auto_hide_cursor()
    window.toggle_fullscreen()
    assert window.player.cursor().shape() != Qt.BlankCursor
    assert not window._cursor_timer.isActive()


@pytest.mark.parametrize("over_controls,dragging", [(True, False), (False, True)])
def test_fullscreen_controls_keep_cursor_usable(window, monkeypatch, over_controls, dragging):
    window.toggle_fullscreen()
    monkeypatch.setattr(window, "_pointer_over_controls", lambda: over_controls)
    window._slider_down = dragging
    window._auto_hide_cursor()
    assert window.player.cursor().shape() != Qt.BlankCursor
    assert window._cursor_timer.isActive()
    assert window.controls_panel.cursor().shape() == Qt.ArrowCursor
    window.toggle_fullscreen()


@pytest.mark.parametrize("address,expected", [
    ("server.local", ("server.local", 8590)),
    (" 127.0.0.1:8590 ", ("127.0.0.1", 8590)),
    ("[::1]:8590", ("::1", 8590)),
    ("[2001:db8::1]", ("2001:db8::1", 8590)),
    ("2001:db8::1", ("2001:db8::1", 8590)),
    ("[fe80::1%eth0]:1234", ("fe80::1%eth0", 1234)),
])
def test_server_address_accepts_hostname_and_ipv6(address, expected):
    assert main_window._parse_server_address(address) == expected
    assert main_window._parse_server_address(main_window._server_address(*expected)) == expected


@pytest.mark.parametrize("address", [
    "", "server:not-a-port", "server:0", "server:65535", "server:-1",
    "server:", ":8590", "http://server:8590", "server name:8590",
    "[::1", "[example]:8590", "[::1]garbage", "[::1]:8590:8591",
])
def test_invalid_server_address_is_handled_before_connection(window, monkeypatch, address):
    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    window.host_edit.setText(address)

    async def scenario():
        await window.on_connect()
        assert window.client is None
        assert errors and errors[0][0] == "Invalid server address"

    asyncio.run(scenario())


def test_volume_controls_follow_player_updates_without_feedback(window):
    window.player.volume_changed.emit(125, True)
    assert window.volume_slider.value() == 125
    assert window.mute_btn.isChecked()
    assert not hasattr(window.player, "volume")
    window.volume_slider.setValue(75)
    assert window.player.volume == 75
    window.mute_btn.setChecked(False)
    assert not window.player.muted


@pytest.mark.parametrize("has_subtitles", [False, True])
def test_local_original_attachment_follows_cached_auxiliary_metadata(window, has_subtitles):
    class LocalClient(FakeSessionClient):
        async def open_session(self, config):
            session = await super().open_session(config)
            self.track = SimpleNamespace(
                time_base=Fraction(1, 1000), average_rate=Fraction(24),
                duration_seconds=lambda: 120.0, chapters=lambda: [],
                has_audio_tracks=False, has_auxiliary_tracks=has_subtitles,
            )
            return session

    async def scenario():
        window.client = LocalClient()
        await window._start_session("/tmp/original.mkv", source="uplink")
        assert window.player.started[3] == ("/tmp/original.mkv" if has_subtitles else None)
        assert not window.player.source_has_audio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "has_audio,has_auxiliary,attach,attach_as_audio",
    [
        (False, False, False, False),
        (False, True, True, False),
        (True, True, True, True),
        (None, None, True, True),  # old servers omit the optional metadata
    ],
)
def test_server_original_attachment_follows_confirmed_source_metadata(
    window, has_audio, has_auxiliary, attach, attach_as_audio,
):
    class ExternalClient(FakeSessionClient):
        async def open_session(self, config):
            session = await super().open_session(config)
            # The server may fall back despite the requested muxed mode.
            session.aux_tracks = "external"
            if has_audio is not None:
                session.source_has_audio = has_audio
            if has_auxiliary is not None:
                session.source_has_auxiliary = has_auxiliary
            return session

    async def scenario():
        window.client = ExternalClient()
        window._server_caps = {"muxed_aux_tracks": True}
        await window._start_session("Shows/original.mkv", source="server_file")
        assert window.client.opened_config.aux_tracks == "muxed"
        expected = window.client.media_url("Shows/original.mkv") if attach else None
        assert window.player.started[3] == expected
        assert window.player.source_has_audio is attach_as_audio

    asyncio.run(scenario())
