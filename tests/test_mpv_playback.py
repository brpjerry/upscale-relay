"""Desktop player state across keyboard actions and stream transitions."""

import os
import asyncio
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("qasync")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from desktop_client.mpv_view import MpvPlayerView
from desktop_client.options import DesktopOptions
from relay_protocol import FLAG_DISCONTINUITY, FLAG_EOS, MediaPacket


def test_keyboard_pause_intent_survives_epoch_release():
    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-playback",
    ))
    try:
        player.pause_requested.connect(lambda: player.set_paused(True))
        player.keyPressEvent(QKeyEvent(
            QKeyEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " ",
        ))
        player._restart_seen = True
        player._external_ready = True
        player._prebuffer_ready = True
        player._maybe_release_epoch()
        assert player._caller_paused
        assert player.mpv.pause
    finally:
        player.stop()
        player.mpv.terminate()
        player.close()


def test_paused_relay_pipe_survives_network_timeout_and_still_delivers_eof(tmp_path, local_player):
    """A quiet live pipe must stay readable until protocol EOS, even while paused."""
    from fractions import Fraction
    from qt_helpers import playback_loop
    from upscale_cli.sample import make_sample

    path = tmp_path / "pause.mkv"
    make_sample(str(path), frames=288, width=64, height=64, fps=24)
    data = path.read_bytes()
    player = local_player
    # Speed up the former 60-second failure without changing the real TCP path.
    player.mpv.network_timeout = 0.2
    player.mpv.demuxer_readahead_secs = 30
    finished, errors = [], []
    player.finished.connect(lambda: finished.append(True))
    player.failed.connect(errors.append)

    async def wait_until(predicate):
        async with asyncio.timeout(8):
            while not predicate():
                assert not errors, errors
                await asyncio.sleep(0.025)

    async def scenario():
        queue = asyncio.Queue()
        queue.put_nowait(MediaPacket(data[:len(data) // 2], flags=FLAG_DISCONTINUITY))
        player.start(SimpleNamespace(downlink_container="matroska"), queue, Fraction(1, 1000))
        try:
            await wait_until(lambda: (player.mpv.time_pos or 0) > 0.1)
            player.set_paused(True)
            position = player.mpv.time_pos
            await asyncio.sleep(1)
            assert abs(player.mpv.time_pos - position) < 0.1
            queue.put_nowait(MediaPacket(data[len(data) // 2:]))
            queue.put_nowait(MediaPacket(b"", flags=FLAG_EOS))
            player.mpv.speed = 4
            player.set_paused(False)
            await wait_until(lambda: (player.mpv.time_pos or 0) > 10)
            await wait_until(lambda: finished)
            assert not errors
            # Local playback must restore the ordinary network timeout.
            await player.play_local(str(path), paused=True)
            assert player.mpv.network_timeout == pytest.approx(0.2)
        finally:
            player.stop()
            await asyncio.sleep(0)

    with playback_loop(QApplication.instance()) as loop:
        loop.run_until_complete(scenario())


@pytest.mark.parametrize("visible,exposed,expect_skip", [
    (True, True, False), (True, False, True), (False, True, True),
])
def test_frame_notifications_acknowledge_unpresented_frames(monkeypatch, visible, exposed, expect_skip):
    from desktop_client import mpv_view
    calls = []
    context = object()
    monkeypatch.setattr(mpv_view, "QOpenGLContext", SimpleNamespace(currentContext=lambda: context))
    render = SimpleNamespace(
        update=lambda: True,
        render=lambda **kw: calls.append(kw),
    )
    player = SimpleNamespace(
        _ctx=render,
        isVisible=lambda: visible,
        window=lambda: SimpleNamespace(windowHandle=lambda: SimpleNamespace(isExposed=lambda: exposed)),
        update=lambda: calls.append("paint"),
        makeCurrent=lambda: calls.append("current"),
        doneCurrent=lambda: calls.append("done"),
        context=lambda: context,
        _report_display=False,
    )
    player._presenting = lambda: MpvPlayerView._presenting(player)
    MpvPlayerView._on_frame_ready(player)
    assert calls == (["current", {"skip_rendering": True}, "done"] if expect_skip else ["paint"])


def test_display_sync_hands_timing_to_audio_while_the_window_is_hidden(monkeypatch):
    """Display sync takes every render call as one refresh. Hidden frames are
    acknowledged at once, so with the rate still reported mpv ran the video
    ~3x realtime off-screen and froze it near 2 fps for a minute on return."""
    from desktop_client import mpv_view
    context = object()
    monkeypatch.setattr(mpv_view, "QOpenGLContext", SimpleNamespace(currentContext=lambda: context))
    exposed = [True]
    sent = []
    player = SimpleNamespace(
        _ctx=SimpleNamespace(update=lambda: True, render=lambda **kw: None),
        isVisible=lambda: True,
        window=lambda: SimpleNamespace(windowHandle=lambda: SimpleNamespace(isExposed=lambda: exposed[0])),
        update=lambda: None, makeCurrent=lambda: None, doneCurrent=lambda: None, context=lambda: context,
        options=SimpleNamespace(headless=False), _display_rate=0.0, _report_display=False,
        screen=lambda: SimpleNamespace(refreshRate=lambda: 120.0),
    )
    player.mpv = type("Props", (), {"__setitem__": lambda _self, key, value: sent.append(value)})()
    player._presenting = lambda: MpvPlayerView._presenting(player)
    player._report_display_rate = lambda: MpvPlayerView._report_display_rate(player)
    MpvPlayerView.set_display_rate_reporting(player, True)
    MpvPlayerView._on_frame_ready(player)
    assert sent == [120.0]
    exposed[0] = False                                  # another workspace
    for _ in range(3):
        MpvPlayerView._on_frame_ready(player)
    assert sent == [120.0, 0.0]                         # audio timing, told once
    exposed[0] = True
    MpvPlayerView._on_frame_ready(player)
    assert sent == [120.0, 0.0, 120.0]                  # paced by the display again


def test_player_loads_input_conf_bindings_but_never_mpv_conf(tmp_path, monkeypatch):
    """The player's mpv options are its own settings; only key bindings come
    from mpv's folder. An mpv.conf beside input.conf changes nothing."""
    folder = tmp_path / "mpv"
    folder.mkdir()
    (folder / "mpv.conf").write_text("volume=37\nvideo-sync=display-resample\nscreenshot-format=jpg\n")
    (folder / "input.conf").write_text("C add panscan 1.0\n")
    monkeypatch.setenv("MPV_HOME", str(folder))
    QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-config-free", input_conf_path=folder / "input.conf",
    ))
    try:
        bindings = {(b["key"], b["cmd"]) for b in player.mpv.input_bindings}
        assert ("C", "add panscan 1.0") in bindings
        assert player.mpv.volume == 100
        assert player.mpv.video_sync == "audio"
        assert player.mpv.resume_playback is False
    finally:
        player.stop()
        player.mpv.terminate()
        player.close()


def test_user_input_conf_is_found_where_mpv_looks(tmp_path):
    from desktop_client.mpv_view import user_input_conf
    home = tmp_path / "home"
    (home / "mpv").mkdir(parents=True)
    (home / "mpv" / "input.conf").write_text("")
    assert user_input_conf({"MPV_HOME": str(home / "mpv")}, "linux") == home / "mpv" / "input.conf"
    assert user_input_conf({"XDG_CONFIG_HOME": str(home)}, "linux") == home / "mpv" / "input.conf"
    assert user_input_conf({"APPDATA": str(home)}, "win32") == home / "mpv" / "input.conf"
    assert user_input_conf({"MPV_HOME": str(tmp_path / "missing")}, "linux") is None


def test_local_playback_ignores_standalone_mpv_resume_entries(tmp_path):
    """mpv's own resume read standalone mpv's watch-later entry for a local
    file: it deleted the entry and applied its volume, panscan and delays."""
    import hashlib
    from qt_helpers import playback_loop
    from upscale_cli.sample import make_sample

    path = tmp_path / "original.mkv"
    make_sample(str(path), frames=48, width=64, height=64, fps=24)
    watch_later = tmp_path / "watch_later"
    watch_later.mkdir()
    entry = watch_later / hashlib.md5(str(path).encode()).hexdigest().upper()
    entry.write_text("start=1.000000\nvolume=37\npanscan=0.400000\nsub-delay=1.500000\n")
    QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-resume",
    ))
    player.mpv["watch-later-dir"] = str(watch_later)
    volume = player.mpv.volume

    async def scenario():
        try:
            await player.play_local(str(path), paused=True)
            assert player.mpv.volume == volume
            assert player.mpv.panscan == 0
            assert player.mpv.sub_delay == 0
            assert player.mpv.time_pos < 0.5
            assert entry.exists()  # standalone mpv keeps its resume point
        finally:
            player.stop()
            await asyncio.sleep(0)

    try:
        with playback_loop(QApplication.instance()) as loop:
            loop.run_until_complete(scenario())
    finally:
        player.mpv.terminate()
        player.close()


def test_local_playback_reports_tracks_position_and_accepts_transport(tmp_path):
    from upscale_cli.sample import make_sample

    path = tmp_path / "original.mkv"
    make_sample(str(path), frames=144, width=64, height=64, fps=24)
    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-local-playback",
    ))
    positions = []
    track_reports = []
    output_reports = []
    player.position_changed.connect(positions.append)
    player.audio_track_list_changed.connect(lambda tracks, selected: track_reports.append(tracks))
    player.volume_changed.connect(lambda volume, muted: output_reports.append((volume, muted)))

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.05)

    async def scenario():
        try:
            await player.play_local(str(path), 1.0, paused=False)
            # No settling or telemetry wait: fallback's caller can immediately
            # pause and seek after play_local returns.
            player.set_paused(True)
            player.seek_local(2.0)
            await wait_until(lambda: positions and track_reports and abs(positions[-1] - 2.0) < 0.1)
            assert player.mpv.pause
            assert 1.9 <= positions[-1] <= 2.1
            # mpv key bindings/config can change output independently of Qt.
            player.mpv.volume = 37
            player.mpv.mute = True
            await wait_until(lambda: output_reports[-1:] == [(37, True)])
            player.set_volume(65)
            player.set_muted(False)
            assert player.audio_output_state() == (65, False)
            player.seek_local(3.0)
            await wait_until(lambda: positions[-1] >= 2.9)
            assert player.mpv.pause  # seeking preserves caller pause intent
            player.set_paused(False)
            await wait_until(lambda: positions[-1] > 3.2)
            assert player._stats_task is not None and not player._stats_task.done()
        finally:
            player.stop()
            await asyncio.sleep(0)

    try:
        asyncio.run(scenario())
    finally:
        player.mpv.terminate()
        player.close()


@pytest.fixture
def local_player():
    app = QApplication.instance() or QApplication([])
    player = MpvPlayerView(options=DesktopOptions(
        headless=True, settings_scope="test-mpv-local-readiness",
    ))
    yield player
    player.stop()
    player.mpv.terminate()
    player.close()


def test_periodic_stats_read_observed_properties_not_native_ones(tmp_path, local_player):
    """A synchronous property read waits for mpv's core, which can be waiting
    for the GUI thread to render: two such reads in one stats tick stalled
    ~210 ms under display sync. Ticks read what mpv's event thread observed."""
    from qt_helpers import playback_loop
    from upscale_cli.sample import make_sample

    path = tmp_path / "stats.mkv"
    make_sample(str(path), frames=240, width=64, height=64, fps=24)
    player = local_player
    positions = []
    player.position_changed.connect(positions.append)

    async def scenario():
        await player.play_local(str(path))
        async with asyncio.timeout(5):
            while not (player._tracks_reported and positions):
                await asyncio.sleep(0.025)
        reads = []
        native = player.mpv._get_property
        player.mpv._get_property = lambda name, *args, **kwargs: (
            reads.append(name), native(name, *args, **kwargs))[1]
        try:
            ticks = len(positions)
            await asyncio.sleep(1.2)
        finally:
            del player.mpv._get_property
            player.stop()  # cancel the stats task while this loop still runs
            await asyncio.sleep(0)
        assert len(positions) >= ticks + 2
        assert positions[-1] > positions[ticks - 1]  # live values, not a stale cache
        assert reads == []

    with playback_loop(QApplication.instance()) as loop:
        loop.run_until_complete(scenario())


def test_idle_inhibition_tracks_native_pause_eof_and_stop(tmp_path, local_player):
    from qt_helpers import playback_loop
    from upscale_cli.sample import make_sample

    path = tmp_path / "idle.mkv"
    make_sample(str(path), frames=48, width=64, height=64, fps=24)
    player = local_player
    states = []
    player._idle_inhibitor = SimpleNamespace(set_active=states.append)

    async def wait_until(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.025)

    async def scenario():
        try:
            await player.play_local(str(path), paused=True)
            await asyncio.sleep(0.05)
            assert not states[-1]
            # Native property changes cover mpv input.conf bindings as well as
            # our own toolbar's pause command.
            player.mpv.pause = False
            await wait_until(lambda: states[-1])
            player.mpv.pause = True
            await wait_until(lambda: not states[-1])
            player.mpv.pause = False
            await wait_until(lambda: states[-1])
            await wait_until(lambda: not states[-1])  # natural EOF
            await player.play_local(str(path))
            await wait_until(lambda: states[-1])
            player.stop()
            assert not states[-1]  # release before queued native stop events
            await asyncio.sleep(0.1)
            assert not states[-1]
        finally:
            player.stop()

    with playback_loop(QApplication.instance()) as loop:
        loop.run_until_complete(scenario())


@pytest.mark.parametrize("cancel_task", [False, True])
def test_stop_or_cancel_pending_local_load_cleans_up(local_player, monkeypatch, cancel_task):
    player = local_player

    async def scenario():
        loading = asyncio.Event()
        monkeypatch.setattr(type(player.mpv), "loadfile", lambda *a, **kw: loading.set())
        task = asyncio.create_task(player.play_local("pending.mkv"))
        await asyncio.wait_for(loading.wait(), 2)
        assert not task.done()
        if cancel_task:
            task.cancel()
        else:
            player.stop()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert not player._local_playback
        assert player._local_load_ready is None
        assert player._stats_task is None

    asyncio.run(scenario())


def test_stop_during_settle_prevents_late_local_load(local_player, monkeypatch):
    player = local_player
    loaded = []
    monkeypatch.setattr(type(player.mpv), "loadfile", lambda *a, **kw: loaded.append(True))

    async def scenario():
        task = asyncio.create_task(player.play_local("superseded.mkv"))
        await asyncio.sleep(0)
        player.stop()
        await asyncio.wait_for(task, 1)
        assert loaded == []
        assert player._stats_task is None

    asyncio.run(scenario())


def test_pause_and_seek_during_local_load_are_applied_when_ready(tmp_path, local_player, monkeypatch):
    from upscale_cli.sample import make_sample
    path = tmp_path / "original.mkv"
    make_sample(str(path), frames=144, width=64, height=64, fps=24)
    player = local_player
    native_load = player.mpv.loadfile

    async def scenario():
        loading = asyncio.Event()
        command = []

        def delay_load(_self, *args, **kwargs):
            command.append((args, kwargs))
            loading.set()

        monkeypatch.setattr(type(player.mpv), "loadfile", delay_load)
        task = asyncio.create_task(player.play_local(str(path), paused=False))
        await asyncio.wait_for(loading.wait(), 2)
        player.set_paused(True)
        player.seek_local(2.0)
        native_load(*command[0][0], **command[0][1])
        await asyncio.wait_for(task, 5)
        assert player.mpv.pause
        async with asyncio.timeout(5):
            while abs((player.mpv.time_pos or 0) - 2.0) > 0.1:
                await asyncio.sleep(0.02)
        player.stop()

    asyncio.run(scenario())


def test_unreadable_local_file_fails_without_waiting_for_timeout(tmp_path, local_player):
    async def scenario():
        with pytest.raises(RuntimeError, match="Local media load ended"):
            await asyncio.wait_for(local_player.play_local(str(tmp_path / "missing.mkv")), 5)
        assert not local_player._local_playback
        assert local_player._stats_task is None

    asyncio.run(scenario())


class ConsumerProbe:
    STARTUP_PACKETS = 1

    def __init__(self, epoch, on_load=None):
        self.options = SimpleNamespace(trace=False)
        self.client = SimpleNamespace(epoch=epoch)
        self.errors = []
        self.failed = SimpleNamespace(emit=self.errors.append)
        self.buffers = []
        self.on_load = on_load
        self.finished_buffers = asyncio.Queue()

    async def _load_stream(self):
        chunks = []
        completed = []

        def finish():
            completed.append(True)
            self.finished_buffers.put_nowait(len(self.buffers))

        self._buffer = SimpleNamespace(feed=chunks.append, finish=finish)
        self.buffers.append((chunks, completed))
        self._fed = 0
        self._prebuffer_ready = False
        if self.on_load is not None:
            await self.on_load(self)

    def _maybe_release_epoch(self):
        pass


async def consume_through_epoch_eos(player, queue):
    task = asyncio.create_task(MpvPlayerView._consume(player, queue))
    try:
        await asyncio.wait_for(player.finished_buffers.get(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_stale_downlink_payload_and_eof_cannot_touch_current_stream():
    async def scenario():
        player = ConsumerProbe(epoch=2)
        queue = asyncio.Queue()
        for packet in (
            MediaPacket(b"", flags=FLAG_EOS, epoch=1),
            MediaPacket(b"old-header", flags=FLAG_DISCONTINUITY, epoch=1),
            MediaPacket(b"old-body", epoch=1),
            MediaPacket(b"current-header", flags=FLAG_DISCONTINUITY, epoch=2),
            MediaPacket(b"current-body", epoch=2),
            MediaPacket(b"", flags=FLAG_EOS, epoch=2),
        ):
            queue.put_nowait(packet)
        await consume_through_epoch_eos(player, queue)
        assert player.buffers == [([b"current-header", b"current-body"], [True])]
        assert player.errors == []

    asyncio.run(scenario())


def test_seek_during_reload_drops_the_superseded_header():
    async def newer_seek_during_load(player):
        if len(player.buffers) == 2:
            await asyncio.sleep(0)
            player.client.epoch = 2

    async def scenario():
        player = ConsumerProbe(epoch=1, on_load=newer_seek_during_load)
        queue = asyncio.Queue()
        for packet in (
            MediaPacket(b"first-header", flags=FLAG_DISCONTINUITY, epoch=1),
            MediaPacket(b"superseded-header", flags=FLAG_DISCONTINUITY, epoch=1),
            MediaPacket(b"", flags=FLAG_EOS, epoch=1),
            MediaPacket(b"latest-header", flags=FLAG_DISCONTINUITY, epoch=2),
            MediaPacket(b"", flags=FLAG_EOS, epoch=2),
        ):
            queue.put_nowait(packet)
        await consume_through_epoch_eos(player, queue)
        assert player.buffers == [([b"first-header"], []), ([], []), ([b"latest-header"], [True])]
        assert player.errors == []

    asyncio.run(scenario())


def test_seek_before_first_header_reopens_the_retired_pipe():
    async def seek_before_header(player):
        if len(player.buffers) == 1:
            player._buffer = None

    async def scenario():
        player = ConsumerProbe(epoch=2, on_load=seek_before_header)
        queue = asyncio.Queue()
        queue.put_nowait(MediaPacket(b"latest-header", flags=FLAG_DISCONTINUITY, epoch=2))
        queue.put_nowait(MediaPacket(b"", flags=FLAG_EOS, epoch=2))
        await consume_through_epoch_eos(player, queue)
        assert player.buffers == [([], []), ([b"latest-header"], [True])]

    asyncio.run(scenario())


def test_seek_after_network_eos_reloads_the_next_epoch():
    async def scenario():
        player = ConsumerProbe(epoch=0)
        queue = asyncio.Queue()
        queue.put_nowait(MediaPacket(b"first-epoch", flags=FLAG_DISCONTINUITY, epoch=0))
        queue.put_nowait(MediaPacket(b"", flags=FLAG_EOS, epoch=0))
        task = asyncio.create_task(MpvPlayerView._consume(player, queue))
        try:
            assert await asyncio.wait_for(player.finished_buffers.get(), 1) == 1
            assert not task.done()  # the network finished before playback did
            assert player.buffers == [([b"first-epoch"], [True])]

            player.client.epoch = 1
            queue.put_nowait(MediaPacket(b"seek-header", flags=FLAG_DISCONTINUITY, epoch=1))
            queue.put_nowait(MediaPacket(b"seek-body", epoch=1))
            queue.put_nowait(MediaPacket(b"", flags=FLAG_EOS, epoch=1))
            assert await asyncio.wait_for(player.finished_buffers.get(), 1) == 2
            assert not task.done()
            assert player.buffers == [
                ([b"first-epoch"], [True]),
                ([b"seek-header", b"seek-body"], [True]),
            ]
            assert player.errors == []

            queue.put_nowait(None)  # transport closure still ends the consumer
            await asyncio.wait_for(task, 1)
            assert player.errors == ["downlink closed"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_stale_terminal_callbacks_cannot_complete_new_load(local_player):
    player = local_player
    finished, failed = [], []
    player.finished.connect(lambda: finished.append(True))
    player.failed.connect(failed.append)
    player._reloading = False
    generation = player._load_generation
    player._on_terminal(generation - 1, True, "old EOF")
    player._on_terminal(generation - 1, False, "old failure")
    assert not finished and not failed
    player._on_terminal(generation, False, "missing relay EOS")
    assert not finished
    assert failed == ["missing relay EOS"]


def test_native_eof_without_current_relay_eos_is_failure(tmp_path, local_player):
    from fractions import Fraction
    from qt_helpers import playback_loop
    from upscale_cli.sample import make_sample
    path = tmp_path / "incomplete.mkv"
    make_sample(str(path), frames=24, width=64, height=64, fps=24)
    player = local_player
    finished, errors = [], []
    player.finished.connect(lambda: finished.append(True))
    player.failed.connect(errors.append)
    async def scenario():
        queue = asyncio.Queue()
        queue.put_nowait(MediaPacket(path.read_bytes(), flags=FLAG_DISCONTINUITY))
        player.start(SimpleNamespace(downlink_container="matroska"), queue, Fraction(1, 1000))
        try:
            async with asyncio.timeout(5):
                while not player._fed:
                    await asyncio.sleep(0.01)
                player._buffer.finish()  # transport ends, but protocol never sent EOS
                while not errors:
                    await asyncio.sleep(0.02)
            assert errors == ["Stream ended without confirmed relay EOS"]
            assert not finished
        finally:
            player.stop()
            await asyncio.sleep(0)
    with playback_loop(QApplication.instance()) as loop:
        loop.run_until_complete(scenario())
