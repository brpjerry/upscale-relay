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
            await player.play_local(str(path), 1.0, paused=True)
            await wait_until(lambda: positions and track_reports)
            assert player.mpv.pause
            assert 0.9 <= positions[-1] <= 1.1
            # mpv key bindings/config can change output independently of Qt.
            player.mpv.volume = 37
            player.mpv.mute = True
            await wait_until(lambda: output_reports[-1:] == [(37, True)])
            player.set_volume(65)
            player.set_muted(False)
            assert player.audio_output_state() == (65, False)
            player.seek_local(2.0)
            await wait_until(lambda: positions[-1] >= 1.9)
            assert player.mpv.pause  # seeking preserves caller pause intent
            player.set_paused(False)
            await wait_until(lambda: positions[-1] > 2.2)
            assert player._stats_task is not None and not player._stats_task.done()
        finally:
            player.stop()
            await asyncio.sleep(0)

    try:
        asyncio.run(scenario())
    finally:
        player.mpv.terminate()
        player.close()


class ConsumerProbe:
    STARTUP_PACKETS = 1

    def __init__(self, epoch, on_load=None):
        self.options = SimpleNamespace(trace=False)
        self.client = SimpleNamespace(epoch=epoch)
        self.errors = []
        self.failed = SimpleNamespace(emit=self.errors.append)
        self.buffers = []
        self.on_load = on_load

    async def _load_stream(self):
        chunks = []
        completed = []
        self._buffer = SimpleNamespace(feed=chunks.append, finish=lambda: completed.append(True))
        self.buffers.append((chunks, completed))
        self._fed = 0
        self._prebuffer_ready = False
        if self.on_load is not None:
            await self.on_load(self)

    def _maybe_release_epoch(self):
        pass


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
        await MpvPlayerView._consume(player, queue)
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
        await MpvPlayerView._consume(player, queue)
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
        await MpvPlayerView._consume(player, queue)
        assert player.buffers == [([], []), ([b"latest-header"], [True])]

    asyncio.run(scenario())
