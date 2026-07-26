"""Seek-path instrumentation and the keyframe-accuracy escape hatch.

Backs docs/SEEK_LATENCY_PLAN.md: every post-seek timing claim should come from
`PipelineStats.last_seek` rather than from client-side inference. The session
here is `server_file`, which is the topology the field measurement used — the
server owns the demuxer, so its seek lands on the source keyframe and the
decode stage walks the keyframe-to-target span.
"""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import av
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay_client_core import RelayClient, SessionConfig
from relay_server import session as session_mod
from relay_server.pipeline import SeekTrace
from relay_server.server import RelayServer

from test_streaming import (  # noqa: E402  (shared helpers)
    collect_downlink,
    demux_downlink_pts,
    free_port_pair,
)

# The sample clip is 90 frames at 30 fps in Matroska's 1/1000 time base, so
# pts are milliseconds (0..2967) and the only keyframe is at 0 (x264 keyint
# 250). Seeking two thirds in therefore produces the case the plan is about:
# the whole keyframe-to-target span has to be decoded and thrown away.
TARGET_MS = 2000


@pytest.fixture()
def library(tmp_path) -> Path:
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    root = tmp_path / "library"
    (root / "Shows").mkdir(parents=True)
    shutil.copy2(sample, root / "Shows" / "Sample.mkv")
    return root


def source_pts(root: Path) -> list[int]:
    with av.open(str(root / "Shows" / "Sample.mkv")) as container:
        stream = container.streams.video[0]
        return sorted(p.pts for p in container.demux(stream) if p.pts is not None)


async def _seek_session(root: Path, seek_discard_max_s: float | None):
    """Open a server_file session, seek to TARGET_MS, drain to EOS."""
    server = RelayServer(str(ROOT / "models"), free_port_pair(),
                         library_root=str(root),
                         seek_discard_max_s=seek_discard_max_s)
    await server.start()
    client = RelayClient("127.0.0.1", server.port)
    try:
        await client.connect()
        await client.open_session(SessionConfig(
            path="Shows/Sample.mkv", source="server_file", model="passthrough",
            display_w=320, display_h=180,
        ))
        await client.attach_media()
        await client.play()
        await collect_downlink(client, stop_after=5)

        session = next(iter(server.sessions.values()))
        await client.seek(TARGET_MS)
        packets = await collect_downlink(client)
        return session.pipeline.stats.last_seek, packets, session
    finally:
        await client.teardown()
        await server.stop()


def test_seek_trace_attributes_the_discard_window(library):
    """The point of step 1: the server, not the client, says where the
    post-seek time went."""

    async def scenario():
        trace, packets, session = await _seek_session(library, None)
        expected_discarded = len([p for p in source_pts(library) if p < TARGET_MS])

        assert trace is not None, "no seek trace recorded"
        assert trace.epoch == 1 and trace.target_pts == TARGET_MS
        assert trace.mode == "accurate"

        # The source seek lands on the keyframe at or before the target; every
        # frame in between is decoded and dropped before the inference queue.
        assert trace.keyframe_pts == 0
        assert trace.keyframe_gap_s == pytest.approx(TARGET_MS / 1000, abs=0.05)
        assert trace.frames_discarded == expected_discarded
        assert trace.discard_decode_ms > 0

        # Timeline is monotonic and complete.
        assert trace.flush_seen_ms is not None
        assert 0 <= trace.flush_seen_ms <= trace.first_frame_ms <= trace.first_packet_ms
        assert trace.first_frame_pts >= TARGET_MS
        # Decoding the discard window is what the wait to the first frame is.
        assert trace.discard_decode_ms <= trace.first_frame_ms

        # /status surfaces it for anyone debugging a live session.
        reported = session.status()["pipeline"]["last_seek"]
        assert reported["frames_discarded"] == expected_discarded
        assert reported["mode"] == "accurate"
        json.dumps(reported)  # must stay JSON-serialisable

        # Frame accuracy is unchanged: nothing before the target is emitted.
        assert min(demux_downlink_pts(packets)) >= TARGET_MS

    asyncio.run(scenario())


def test_seek_discard_max_s_starts_at_the_keyframe(library):
    """With the threshold armed, a long keyframe-to-target span is skipped:
    no discard decode at all, and the epoch starts early instead."""

    async def scenario():
        trace, packets, _ = await _seek_session(library, 0.5)

        assert trace.mode == "keyframe"
        assert trace.keyframe_pts == 0
        assert trace.frames_discarded == 0
        assert trace.first_frame_pts == 0

        got = demux_downlink_pts(packets)
        # Keyframe-accurate: playback starts at the keyframe, before the
        # target, and still runs to the end of the file.
        assert min(got) == 0
        assert got == source_pts(library)

    asyncio.run(scenario())


def test_short_discard_window_stays_frame_accurate(library):
    """A threshold only trades accuracy away when the span is long enough to
    be worth it — a near keyframe keeps the exact seek."""

    async def scenario():
        trace, packets, _ = await _seek_session(library, 30.0)

        assert trace.mode == "accurate"
        assert trace.frames_discarded > 0
        assert min(demux_downlink_pts(packets)) >= TARGET_MS

    asyncio.run(scenario())


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_str(self, raw):
        self.sent.append(json.loads(raw))


def test_seek_progress_ticks_until_the_first_packet(monkeypatch):
    """A seek whose first downlink bytes are slow narrates itself; one that
    lands promptly stays silent."""
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INITIAL_DELAY_S", 0.02)
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INTERVAL_S", 0.02)

    async def scenario():
        ws = _FakeWS()
        session = session_mod.Session(ws, {})
        session.epoch = 3
        session.state = session_mod.State.PLAYING
        trace = SeekTrace(epoch=3, target_pts=9000,
                          requested_at=time.perf_counter())
        trace.keyframe_pts = 4500
        task = asyncio.create_task(session._seek_progress_loop(trace, 3))

        await asyncio.sleep(0.15)
        ticks = [m for m in ws.sent if m["type"] == "seek_progress"]
        assert ticks, "slow seek sent no progress"
        assert ticks[0]["epoch"] == 3
        assert ticks[0]["target_pts"] == 9000
        assert ticks[0]["keyframe_pts"] == 4500

        # First packet lands -> the ticker stops on its own.
        trace.first_packet_ms = 150.0
        await asyncio.wait_for(task, timeout=1.0)
        settled = len(ws.sent)
        await asyncio.sleep(0.1)
        assert len(ws.sent) == settled

    asyncio.run(scenario())


def test_seek_progress_stops_when_the_epoch_moves_on(monkeypatch):
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INITIAL_DELAY_S", 0.02)
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INTERVAL_S", 0.02)

    async def scenario():
        ws = _FakeWS()
        session = session_mod.Session(ws, {})
        session.epoch = 3
        session.state = session_mod.State.PLAYING
        trace = SeekTrace(epoch=3, target_pts=9000,
                          requested_at=time.perf_counter())
        task = asyncio.create_task(session._seek_progress_loop(trace, 3))
        await asyncio.sleep(0.1)
        assert any(m["type"] == "seek_progress" for m in ws.sent)

        session.epoch = 4  # a newer seek superseded this one
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())


def test_seek_progress_gives_up_on_a_seek_that_never_produces(monkeypatch, caplog):
    """A seek past the last usable keyframe never yields a packet; the ticker
    must stop and say so rather than narrate forever."""
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_INTERVAL_S", 0.01)
    monkeypatch.setattr(session_mod, "SEEK_PROGRESS_MAX_S", 0.05)

    async def scenario():
        ws = _FakeWS()
        session = session_mod.Session(ws, {})
        session.epoch = 1
        session.state = session_mod.State.PLAYING
        trace = SeekTrace(epoch=1, target_pts=1, requested_at=time.perf_counter())
        await asyncio.wait_for(session._seek_progress_loop(trace, 1), timeout=2.0)
        assert any(m["type"] == "seek_progress" for m in ws.sent)
        assert "giving up on progress ticks" in caplog.text

    with caplog.at_level("WARNING", logger="relay.session"):
        asyncio.run(scenario())
