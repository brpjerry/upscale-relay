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

import desktop_client.main_window as main_window
from desktop_client.options import DesktopOptions
from desktop_client.mpv_view import _LoopbackStream


class FakePlayer(QWidget):
    stats_changed = Signal(str)
    position_changed = Signal(float)
    track_list_changed = Signal(list, object)
    rebuffering = Signal(bool)
    seek_requested = Signal(float)
    finished = Signal()
    failed = Signal(str)
    fullscreen_toggled = Signal()
    mouse_moved = Signal(int, int)

    def __init__(self, options=None):
        super().__init__()
        self.client = None
        self.started = None

    def start(self, session, queue, time_base, source_path=None, avg_rate=None):
        self.started = (session, queue, time_base, source_path, avg_rate)

    def stop(self):
        pass

    def set_panscan(self, value):
        pass

    def set_paused(self, value):
        pass

    def set_sub_delay(self, value):
        pass

    def select_subtitle(self, sid):
        pass

    def play_local_fallback(self, position_s):
        pass


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

    async def open_session(self, config):
        self.opened_config = config
        self.session = SimpleNamespace(
            downlink_codec="hevc", downlink_width=1920, downlink_height=1080,
            downlink_container="matroska", time_base=Fraction(1, 1000),
            duration_s=120.0, avg_rate=Fraction(24, 1),
            chapters=getattr(self, "chapters", None),
        )
        return self.session

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


def test_open_progress_indicator_toggles(window):
    assert window.open_progress.isHidden()
    window._on_open_progress({"message": "Preparing anime — TensorRT engine", "elapsed_s": 12.0})
    assert not window.open_progress.isHidden()
    assert window.statusBar().currentMessage() == "Preparing anime — TensorRT engine (12 s)"
    window._set_opening(False)
    assert window.open_progress.isHidden()


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
