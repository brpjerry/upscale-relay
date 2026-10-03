"""Feature integration through the retained desktop trees and transport slots."""
import asyncio
from fractions import Fraction
from pathlib import Path

import pytest
pytest.importorskip("PySide6")
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

import desktop_client.main_window as main_window
from desktop_client.playback_state import PlaybackSnapshot
from test_server_library_gui import window, FakeSessionClient, FakeLibraryClient


class LifecycleClient(FakeSessionClient):
    def __init__(self, events, host="media-server", port=8590):
        super().__init__()
        self.events = events
        self.host, self.port = host, port
        self.session = None
        self.epoch = 0
    async def connect(self):
        return {"server_name": "test", "models": [{"name": "passthrough"}]}
    async def open_session(self, config):
        self.events.append(("open", config.path, config.fit_mode))
        session = await super().open_session(config)
        session.duration_s = 1000
        return session
    async def seek(self, pts):
        self.epoch += 1
        self.events.append(("seek", pts))
    async def start_uplink(self):
        self.events.append(("uplink", 0))
    async def play(self):
        self.events.append(("play",))
    async def pause(self):
        self.events.append(("pause",))
    async def teardown(self):
        self.events.append(("closed",))
        self.session = None
    async def close(self):
        await self.teardown()


def setup_lifecycle(window, monkeypatch):
    events = []
    window.client = LifecycleClient(events)
    monkeypatch.setattr(main_window, "RelayClient", lambda host, port: LifecycleClient(events, host, port))
    errors = []
    monkeypatch.setattr(window, "_error", lambda *args: errors.append(args))
    return events, errors


def test_resume_seeks_before_first_load_and_does_not_start_zero_uplink(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    key = window._key_for("server_file", "show.mkv")
    window.history.save(key, 123, 1000)
    original = window.player.start
    def start(*args, **kwargs):
        events.append(("player",))
        return original(*args, **kwargs)
    monkeypatch.setattr(window.player, "start", start)
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        assert events.index(("seek", 123000)) < events.index(("player",)) < events.index(("play",))
        assert ("uplink", 0) not in events
        assert window._pending_seek_s == 123
        window._on_position(0)
        assert window._position_s == 123
        window._on_position(123.1)
        assert window._pending_seek_s is None
        assert not errors
    asyncio.run(scenario())


def test_restart_preserves_pause_tracks_delays_and_latest_settings(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        window._position_s = 234.5
        window._paused = True
        window.audio_delay.setValue(0.3)
        window.sub_delay.setValue(-0.2)
        monkeypatch.setattr(window.player, "track_choices", lambda: {"sub": None, "audio": {"type": "audio", "lang": "ja"}}, raising=False)
        first = asyncio.create_task(window._restart_for_playback_setting())
        await asyncio.sleep(0)
        # A pause change during a queued restart remains the caller's intent.
        await window.on_play_pause()
        window.fit_combo.blockSignals(True)
        window.fit_combo.setCurrentIndex(1)
        window.fit_combo.blockSignals(False)
        second = asyncio.create_task(window._restart_for_playback_setting())
        await asyncio.gather(first, second)
        assert [e for e in events if e[0] == "open"] == [("open", "show.mkv", "fit"), ("open", "show.mkv", "cover")]
        assert ("seek", 234500) in events
        assert not window.player.paused
        assert window.player.remembered_tracks["sub"] is None
        assert window.audio_delay.value() == 0.3
        assert window.sub_delay.value() == -0.2
        assert not errors
    asyncio.run(scenario())


def test_stop_supersedes_pending_open_without_late_player_load(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    async def scenario():
        started = asyncio.Event()
        client = window.client
        real_open = client.open_session
        async def slow(config):
            session = await real_open(config)
            started.set()
            await asyncio.Event().wait()
            return session
        client.open_session = slow
        opening = asyncio.create_task(window._start_session("show.mkv", "server_file"))
        await started.wait()
        await window.on_stop()
        await opening
        assert ("closed",) in events
        assert window.player.started is None
        assert window._session_path is None
        assert not errors
    asyncio.run(scenario())


def test_history_ignores_previews_pending_seek_and_reload(window):
    window._session_source = "uplink"
    window._session_path = "/example.mkv"
    window._history_key = window._key_for("uplink", "/example.mkv")
    window._duration_s = 1000
    window._on_position(100)
    window._save_history()
    window._scrub_preview(800)
    window._on_position(101)
    assert "+700.0s" in window.pos_label.text()
    assert window._position_s == 101
    window._save_history()
    assert window.history.entries[window._history_key].position == 100
    window._cancel_scrub()
    window._arm_pending_seek(800)
    window._save_history()
    assert window.history.entries[window._history_key].position == 100
    window._pending_seek_s = None
    window._transitioning = True
    window._save_history()
    assert window.history.entries[window._history_key].position == 100


def test_scrub_release_seeks_once_cancel_sends_nothing_and_short_seeks_bound(window, monkeypatch):
    seeks = []
    async def seek(target, announce=True):
        seeks.append(target)
    monkeypatch.setattr(window, "_seek_to_seconds", seek)
    async def scenario():
        window._duration_s = 100
        window._position_s = 20
        window.seek_slider.setValue(700)
        window._scrub_preview(700)
        await window.on_seek()
        assert seeks == [70]
        window.seek_slider.setSliderDown(True)
        window._scrub_preview(500)
        window.seek_slider.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        await window.on_seek()
        assert seeks == [70]
    asyncio.run(scenario())


def test_settings_open_never_writes_and_external_changes_defer(window, monkeypatch):
    path = window.options.mpv_config_path
    applied = []
    monkeypatch.setattr(window.player, "apply_defaults", lambda values: applied.append(dict(values)), raising=False)
    assert not path.exists()
    window.playback_settings.show()
    assert not path.exists()
    path.write_text("slang=ja,en\ntscale=custom-value\n")
    window.config_watcher.refresh()
    assert window.mpv_controls["tscale"].currentData() == "custom-value"
    assert not applied
    window._transitioning = True
    window._edit_mpv_default("interpolation", "yes")
    assert not applied
    assert "slang=ja,en" in path.read_text()
    window._transitioning = False
    window._apply_pending_defaults()
    assert applied == [{"interpolation": "yes"}]


def test_nearby_selection_is_explicit_and_does_not_connect(window):
    from desktop_client.discovery import NearbyServer
    window.host_edit.setText("manual.example:9000")
    window._nearby_changed({"a": NearbyServer("Nearby", "::1", 8590)})
    assert window.host_edit.text() == "manual.example:9000"
    window.nearby_menu.actions()[0].trigger()
    assert window.host_edit.text() == "[::1]:8590"
    assert window.client is None
    window._nearby_changed({})
    assert window.host_edit.text() == "[::1]:8590"


def test_autoplay_walks_name_pages_skips_watched_and_retains_partial_resume(window):
    class Paged(FakeLibraryClient):
        async def fetch_library_page(self, path="", *, cursor=None, limit=100, sort=None):
            self.fetches.append((path, cursor, sort))
            name = "b.mkv" if cursor is None else "c.mkv"
            return {"tree": {"children": [{"type": "file", "name": name, "path": f"Shows/{name}"}]},
                    "next_cursor": "next" if cursor is None else None}
    async def scenario():
        window.client = Paged()
        window._server_caps = {"library_sort": ["name", "mtime"]}
        window.settings.browser_sort = "mtime"
        window.history.mark(window._key_for("server_file", "Shows/b.mkv"), True)
        window.history.save(window._key_for("server_file", "Shows/c.mkv"), 80, 1000)
        snapshot = PlaybackSnapshot("server_file", "Shows/a.mkv", 1000, 1000, False)
        assert await window._next_sibling(snapshot, lambda: True) == "Shows/c.mkv"
        assert window.client.fetches == [("Shows", None, "name"), ("Shows", "next", "name")]
        assert window.history.entries[window._key_for("server_file", "Shows/c.mkv")].resume == 80
        snapshot.path = "Shows/c.mkv"
        assert await window._next_sibling(snapshot, lambda: True) is None
    asyncio.run(scenario())


def test_autoplay_is_cancelled_by_new_user_intent(window, monkeypatch):
    setup_lifecycle(window, monkeypatch)
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(window, "_next_sibling", slow)
        window._end_session("end")
        await entered.wait()
        await window._start_session("chosen.mkv", "server_file")
        assert window._session_path == "chosen.mkv"
    asyncio.run(scenario())


def test_browser_restores_expansion_selection_and_name_sort_fallback(window):
    async def scenario():
        client = FakeLibraryClient()
        await window._adopt_connected_client(client, {"server_name": "test", "models": [{"name": "passthrough"}], "library": True})
        folder = window.server_model.item(0)
        await window.on_server_directory_expanded(folder.index())
        window.server_tree.expand(folder.index())
        window.server_tree.setCurrentIndex(folder.child(0).index())
        window.browser_panel.setCurrentWidget(window.server_browser_panel)
        window.settings.browser_sort = "mtime"
        await window.on_refresh_server_library()
        restored = window.server_model.item(0)
        assert window.server_tree.isExpanded(restored.index())
        assert window.server_tree.currentIndex().data(Qt.UserRole) == "Shows/Episode.mkv"
        assert window.browser_panel.currentWidget() is window.server_browser_panel
        assert window._server_sort_kwargs() == {}
        assert window.settings.browser_sort == "mtime"
    asyncio.run(scenario())


def test_stale_library_refresh_cannot_replace_latest_listing(window):
    async def scenario():
        old = asyncio.Event()
        class Client(FakeLibraryClient):
            async def fetch_library_page(self, *args, **kwargs):
                self.fetches.append(True)
                if len(self.fetches) == 1:
                    await old.wait()
                    name = "old.mkv"
                else:
                    name = "new.mkv"
                return {"tree": {"children": [{"name": name, "path": name, "type": "file"}]}, "next_cursor": None}
        window.client = Client()
        window._ensure_server_tab()
        first = asyncio.create_task(window.on_refresh_server_library.__wrapped__(window))
        await asyncio.sleep(0)
        await window.on_refresh_server_library()
        old.set()
        await first
        assert window.server_model.item(0).text() == "new.mkv"
    asyncio.run(scenario())


def test_local_proxy_directories_first_newest_and_index_navigation(window, tmp_path):
    import os
    from qt_helpers import playback_loop
    from PySide6.QtWidgets import QApplication
    for name in ("z.mkv", "A.mkv", "b.mkv"):
        (tmp_path / name).write_text("")
    (tmp_path / "folder").mkdir()
    os.utime(tmp_path / "A.mkv", (1000, 1000))
    os.utime(tmp_path / "b.mkv", (1000, 1000))
    os.utime(tmp_path / "z.mkv", (2000, 2000))
    async def scenario():
        window._set_browse_root(str(tmp_path))
        async with asyncio.timeout(3):
            while window.local_proxy.rowCount(window.tree.rootIndex()) < 4:
                await asyncio.sleep(0.02)
        def names():
            root = window.tree.rootIndex()
            return [window.fs_model.fileName(window.local_proxy.mapToSource(window.local_proxy.index(i, 0, root))) for i in range(4)]
        assert names() == ["folder", "A.mkv", "b.mkv", "z.mkv"]
        window.local_proxy.set_order("mtime")
        assert names() == ["folder", "z.mkv", "A.mkv", "b.mkv"]
        window.on_up_dir()
        assert window.fs_model.filePath(window.local_proxy.mapToSource(window.tree.rootIndex())) == str(tmp_path.parent)
    with playback_loop(QApplication.instance()) as loop:
        loop.run_until_complete(scenario())


def test_restore_loads_pages_with_ten_page_cap_and_missing_path_fallback(window):
    async def scenario():
        class Client(FakeLibraryClient):
            async def fetch_library_page(self, path="", *, cursor=None, limit=100, **kw):
                page = int(cursor or 0)
                self.fetches.append((path, page))
                if not path:
                    children = [{"name": "Shows", "path": "Shows", "type": "directory"}]
                    cursor = None
                else:
                    children = [{"name": f"{page:03}.mkv", "path": f"Shows/{page:03}.mkv", "type": "file"}]
                    cursor = str(page + 1) if page < 20 else None
                return {"tree": {"children": children}, "next_cursor": cursor}
        from desktop_client.history import endpoint_key
        endpoint = endpoint_key("media-server", 8590)
        window.browser_state.servers[endpoint] = {"expanded": ["Shows"], "selected": "Shows/018.mkv"}
        window.browser_state.tab = "Server"
        client = Client()
        await window._adopt_connected_client(client, {"server_name": "test", "models": [{"name": "passthrough"}], "library": True})
        assert len([p for p in client.fetches if p[0] == "Shows"]) == 10
        assert window.server_tree.currentIndex().data(Qt.UserRole) == "Shows"
    asyncio.run(scenario())


def test_shared_sort_control_has_height_with_local_only_browser(window):
    from PySide6.QtWidgets import QApplication
    window._apply_browser_visible(True)
    window.show()
    QApplication.instance().processEvents()  # synchronous test, outside coroutine context
    assert window.browser_panel.tabBar().isHidden()
    assert window.sort_combo.isVisible()
    assert window.sort_combo.height() > 0
    window._apply_browser_visible(False)
    assert not window.sort_combo.isVisible()



def test_skip_buttons_default_to_85_s_and_follow_the_setting(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    assert window.settings.fast_forward_s == 85
    assert window.fast_forward_spin.value() == 85
    assert window.seek_forward_btn.text() == "+1:25"
    assert window.seek_back_btn.text() == "\u22121:25"
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        window._on_position(100.0)
        await window.on_skip(1)
        assert events[-1] == ("seek", 185000)
        window._on_position(185.0)
        window.fast_forward_spin.setValue(30)
        assert window.settings.fast_forward_s == 30
        assert window.seek_forward_btn.text() == "+0:30"
        assert window.seek_back_btn.text() == "\u22120:30"
        await window.on_skip(-1)
        assert events[-1] == ("seek", 155000)
        assert not errors
    asyncio.run(scenario())


def test_skip_amount_clamps(window):
    window.settings.fast_forward_s = 0
    assert window.settings.fast_forward_s == 1
    window.settings.fast_forward_s = 99999
    assert window.settings.fast_forward_s == 3600


def test_chapter_buttons_flank_skip_buttons_and_hide_without_chapters(window):
    from desktop_client.chapters import Chapter
    layout = window.seek_back_btn.parentWidget().layout()
    row = next(layout.itemAt(i).layout() for i in range(layout.count())
               if layout.itemAt(i).layout() and layout.itemAt(i).layout().indexOf(window.seek_back_btn) >= 0)
    order = [row.indexOf(w) for w in (window.chapter_prev_btn, window.seek_back_btn,
                                       window.seek_forward_btn, window.chapter_next_btn)]
    assert order == sorted(order) and order == list(range(order[0], order[0] + 4))
    window._set_chapters([])
    assert window.chapter_next_btn.isHidden() and window.chapter_prev_btn.isHidden()
    assert not window.seek_forward_btn.isHidden()
    window._set_chapters([Chapter(0.0, "Intro"), Chapter(90.0, "Part A")])
    assert not window.chapter_next_btn.isHidden() and not window.chapter_prev_btn.isHidden()


def test_idle_control_loss_reconnects_without_an_error(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    monkeypatch.setattr(main_window, "_RECONNECT_DELAYS_S", (0.0,))
    lost = window.client
    async def scenario():
        window._on_control_lost(lost)
        for _ in range(50):
            if window.client is not lost:
                break
            await asyncio.sleep(0.01)
        assert window.client is not None and window.client is not lost
        assert ("closed",) in events  # the dead client's local resources were released
        assert not errors
        # A stale notification from the replaced client changes nothing.
        current = window.client
        window._on_control_lost(lost)
        await asyncio.sleep(0.05)
        assert window.client is current
    asyncio.run(scenario())


def test_control_loss_during_playback_is_left_to_the_session(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        playing = window.client
        window._on_control_lost(playing)
        await asyncio.sleep(0.05)
        assert window.client is playing
        assert window._session_source == "server_file"
    asyncio.run(scenario())


def test_play_on_a_dead_idle_connection_opens_on_a_fresh_one(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    monkeypatch.setattr(main_window, "_RECONNECT_DELAYS_S", (0.0,))
    dead = window.client
    dead.connected = False
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        assert window.client is not dead
        assert window._session_source == "server_file"
        assert events.index(("closed",)) < events.index(("open", "show.mkv", "fit"))
        assert not errors
    asyncio.run(scenario())


def test_failed_reconnect_shows_disconnected(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    monkeypatch.setattr(main_window, "_RECONNECT_DELAYS_S", (0.0, 0.0))
    class Unreachable(LifecycleClient):
        async def connect(self):
            raise ConnectionError("unreachable")
    monkeypatch.setattr(main_window, "RelayClient", lambda host, port: Unreachable(events, host, port))
    lost = window.client
    async def scenario():
        window._on_control_lost(lost)
        for _ in range(50):
            if window.client is None:
                break
            await asyncio.sleep(0.01)
        assert window.client is None
        assert window.connect_btn.text() == "Connect"
        assert "Lost the connection" in window.statusBar().currentMessage()
        assert not errors
    asyncio.run(scenario())
