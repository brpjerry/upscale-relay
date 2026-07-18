"""Phase-2 integration tests: in-process server + client core.

Covers the phase-2 acceptance cases in docs/PLAN.md: full playthrough PTS equivalence, seek storm,
mid-play disconnect, slow-client backpressure.
"""

import asyncio
import socket
import sys
from pathlib import Path

import av
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay_client_core import RelayClient, SessionConfig
from relay_server.server import RelayServer

SAMPLE = ROOT / "tests" / "_sample_stream.mkv"
FRAMES = 240
FPS = 30


@pytest.fixture(scope="module")
def sample_file() -> str:
    if not SAMPLE.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(SAMPLE), frames=FRAMES, width=320, height=180, fps=FPS)
    return str(SAMPLE)


_next_port = [0]


def free_port_pair() -> int:
    """Find p such that p and p+1 are both free.

    Walks a private range instead of asking the OS for ephemeral ports —
    check-then-use on ephemeral ports races with concurrent tests and with
    the OS handing the same port to someone else.
    """
    import random

    if _next_port[0] == 0:
        _next_port[0] = random.randrange(20000, 40000, 2)
    for _ in range(200):
        p = _next_port[0]
        _next_port[0] += 2
        try:
            with socket.socket() as s1, socket.socket() as s2:
                s1.bind(("127.0.0.1", p))
                s2.bind(("127.0.0.1", p + 1))
            return p
        except OSError:
            continue
    raise RuntimeError("no free port pair")


def source_pts_list(path: str) -> list[int]:
    with av.open(path) as c:
        stream = c.streams.video[0]
        return sorted(p.pts for p in c.demux(stream) if p.pts is not None)


def demux_downlink_pts(packets, source_tb_den: int = 1000) -> list[int]:
    """Demux the newest epoch's container bytes (after the last discontinuity)
    and return sorted PTS in source ticks (the sample MKV's time_base is
    1/1000, matching Matroska's, so ticks are milliseconds)."""
    import io

    start = 0
    for i, p in enumerate(packets):
        if p.discontinuity:
            start = i
    blob = b"".join(p.payload for p in packets[start:] if p.payload)
    out = []
    with av.open(io.BytesIO(blob)) as c:
        stream = c.streams.video[0]
        for pkt in c.demux(stream):
            if pkt.pts is not None:
                out.append(round(float(pkt.pts * stream.time_base) * source_tb_den))
    return sorted(out)


async def start_server() -> RelayServer:
    server = RelayServer(str(ROOT / "models"), free_port_pair())
    await server.start()
    return server


async def collect_downlink(client: RelayClient, stop_after: int | None = None,
                           timeout: float = 60.0):
    """Drain downlink queue until EOS/None; returns list of MediaPackets."""
    packets = []
    q = client.downlink_queue()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        pkt = await asyncio.wait_for(q.get(), timeout=max(0.1, deadline - loop.time()))
        if pkt is None:
            break
        packets.append(pkt)
        # Feed the pacing loop: pretend we're consuming at exactly real time.
        if pkt.pts >= 0:
            client.buffered_ms = 0
        if pkt.eos:
            break
        if stop_after is not None and len(packets) >= stop_after:
            break
    return packets


def test_full_playthrough_pts_equivalence(sample_file):
    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                    display_w=320, display_h=180))
            await client.attach_media()
            await client.start_uplink()
            await client.play()
            packets = await collect_downlink(client)
            assert packets[-1].eos
            assert demux_downlink_pts(packets) == source_pts_list(sample_file)
            # Exactly one discontinuity, on the first packet.
            assert packets[0].discontinuity
            assert not any(p.discontinuity for p in packets[1:])
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_seek_then_stream_from_target(sample_file):
    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                    display_w=320, display_h=180))
            await client.attach_media()
            await client.start_uplink()
            await client.play()
            await collect_downlink(client, stop_after=20)

            target = 120  # 4.0 s in 1/30 ticks
            await client.seek(target)
            packets = await collect_downlink(client)

            assert packets, "no packets after seek"
            assert all(p.epoch == 1 for p in packets)
            data = [p for p in packets if p.payload]
            assert data[0].discontinuity
            # Stream must run to EOS from the target.
            assert packets[-1].eos
            got = demux_downlink_pts(packets)
            assert min(got) >= target
            assert got == [p for p in source_pts_list(sample_file) if p >= target]
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_seek_storm(sample_file):
    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                    display_w=320, display_h=180))
            await client.attach_media()
            await client.start_uplink()
            await client.play()
            await collect_downlink(client, stop_after=5)

            targets = [200, 30, 150, 60, 90]
            for t in targets:
                await client.seek(t)
            packets = await collect_downlink(client)

            final_epoch = len(targets)
            assert all(p.epoch == final_epoch for p in packets), \
                f"stale epochs leaked: {sorted({p.epoch for p in packets})}"
            got = demux_downlink_pts(packets)
            assert min(got) >= targets[-1]
            assert packets[-1].eos
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_midplay_disconnect_cleans_up(sample_file):
    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        await client.connect()
        await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                display_w=320, display_h=180))
        await client.attach_media()
        await client.start_uplink()
        await client.play()
        await collect_downlink(client, stop_after=10)

        # Abrupt kill: close the WS without teardown.
        await client.close()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if not server.sessions:
                break
        assert not server.sessions, "server did not clean up session after disconnect"
        await server.stop()

    asyncio.run(scenario())


def test_stalled_reports_do_not_wedge_pause(sample_file):
    """A reporter that dies holding a stale high buffered_ms must not wedge
    the backpressure pause: while PLAYING the server decays the last report
    by wall time and resumes on its own (seen in the field as the client
    buffer draining to zero with the server paused)."""

    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                    display_w=320, display_h=180))
            # Just above the high watermark: decay crosses the resume
            # threshold ~1.5 s after the last report.
            client.buffered_ms = 11_000
            await client.attach_media()
            await asyncio.sleep(0.3)  # let the report land before packets flow
            await client.start_uplink()
            await client.play()

            session = next(iter(server.sessions.values()))
            paused_seen = False
            for _ in range(100):
                await asyncio.sleep(0.1)
                if session.pipeline and session.pipeline.stats.paused_for_backpressure:
                    paused_seen = True
                    break
            assert paused_seen, "pipeline never paused despite full client buffer"

            # Reporter dies; the stale 11 s value is the server's last word.
            client._report_task.cancel()

            resumed = False
            for _ in range(100):
                await asyncio.sleep(0.1)
                if not session.pipeline.stats.paused_for_backpressure:
                    resumed = True
                    break
            assert resumed, "pipeline stayed wedged on a stale buffer report"

            # With the estimate decaying it never pauses again; drain to EOS.
            q = client.downlink_queue()
            while True:
                pkt = await asyncio.wait_for(q.get(), timeout=60)
                if pkt is None or pkt.eos:
                    break
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_backpressure_pauses_pipeline(sample_file):
    async def scenario():
        server = await start_server()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(path=sample_file, model="passthrough",
                                                    display_w=320, display_h=180))
            # Claim a huge buffer before any data flows: pipeline should pause.
            client.buffered_ms = 60_000
            await client.attach_media()  # first buffer_report goes out on attach
            await asyncio.sleep(0.3)  # let the report land before packets flow
            await client.start_uplink()
            await client.play()

            session = next(iter(server.sessions.values()))
            q = client.downlink_queue()

            paused_seen = False
            for _ in range(100):
                await asyncio.sleep(0.1)
                if session.pipeline and session.pipeline.stats.paused_for_backpressure:
                    paused_seen = True
                    break
            assert paused_seen, "pipeline never paused despite full client buffer"
            frames_at_pause = session.pipeline.stats.frames_out

            # Stay paused: no meaningful progress while buffer stays full.
            await asyncio.sleep(1.0)
            assert session.pipeline.stats.frames_out - frames_at_pause <= 2

            # Release: pipeline resumes and finishes.
            client.buffered_ms = 0
            packets = []
            while True:
                pkt = await asyncio.wait_for(q.get(), timeout=60)
                if pkt is None:
                    break
                packets.append(pkt)
                client.buffered_ms = 0
                if pkt.eos:
                    break
            assert packets[-1].eos
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
