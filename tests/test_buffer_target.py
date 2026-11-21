"""A client chooses how much video the server keeps buffered for it.

open_session.buffer_target_ms sets the session's backpressure watermark and
buffer_report.buffer_target_ms replaces it (docs/PROTOCOL.md §5);
session_opened confirms it, and a server that predates the field keeps 10 s.
"""

import asyncio
import math
import sys
import threading
import time
from pathlib import Path

import pytest

from ports import free_port_pair

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from relay_client_core import RelayClient, SessionConfig
from relay_client_core.client import _confirmed_buffer_target_ms
from relay_protocol import (
    DEFAULT_BUFFER_TARGET_MS,
    FLAG_EOS,
    MIN_BUFFER_TARGET_MS,
    MediaPacket,
    parse_buffer_target_ms,
)
from relay_server.server import RelayServer


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory) -> str:
    from upscale_cli.sample import make_sample

    path = tmp_path_factory.mktemp("buffer-target") / "sample.mkv"
    make_sample(str(path), frames=48, width=64, height=64, fps=24)
    return str(path)


# Bugs caught: 0, False positives: 0
def test_wire_values_have_a_floor_and_non_numbers_are_rejected():
    assert parse_buffer_target_ms(30_000) == 30_000
    assert parse_buffer_target_ms(30_000.4) == 30_000
    assert parse_buffer_target_ms(1) == MIN_BUFFER_TARGET_MS
    assert parse_buffer_target_ms(10**400) == 10**400  # no maximum, past a float's range
    for bad in (True, "30000", None, [30_000], math.nan, math.inf):
        assert parse_buffer_target_ms(bad) is None


# Bugs caught: 0, False positives: 0
def test_client_reads_the_servers_confirmation():
    assert _confirmed_buffer_target_ms(45_000) == 45_000
    # A server that predates the field confirms nothing (and keeps 10 s).
    assert _confirmed_buffer_target_ms(None) is None
    assert _confirmed_buffer_target_ms(True) is None
    assert _confirmed_buffer_target_ms(0) is None


# Bugs caught: 0, False positives: 1
def test_client_refuses_to_send_an_invalid_target():
    async def scenario():
        client = RelayClient("127.0.0.1", 1)  # never connected: checked first
        with pytest.raises(ValueError, match="buffer target"):
            await client.open_session(SessionConfig(path="x.mkv", buffer_target_ms="lots"))

    asyncio.run(scenario())


def _relay(sample_file: str, reported_ms: int, **pipeline_kwargs):
    """Feed the sample through a passthrough pipeline while the client reports
    ``reported_ms`` buffered (paused, so the report does not decay)."""
    from relay_media import VideoTrack
    from relay_server.pipeline import Pipeline
    from test_server_pipeline_media import _config

    track = VideoTrack(sample_file)
    emitted, done, errors = [], threading.Event(), []

    def emit(packet):
        emitted.append(packet)
        if packet.eos:
            done.set()

    pipeline = Pipeline(
        _config(track.open_session_video_dict()), None, "lossless-ffv1", (64, 64),
        emit, errors.append, **pipeline_kwargs,
    )
    pipeline.note_buffer_report(reported_ms)
    for info in track.packets():
        pipeline.feed(track.media_packet(info, 0))
    pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=0))
    track.close()
    return pipeline, emitted, done, errors


# Bugs caught: 0, False positives: 0
def test_pipeline_fills_up_to_the_sessions_target(sample_file):
    # 20 s buffered is past the old fixed 10 s watermark but short of the
    # 30 s this client asked for: the pipeline keeps producing.
    pipeline, emitted, done, errors = _relay(sample_file, 20_000, buffer_target_ms=30_000)
    try:
        assert done.wait(30), "the pipeline paused below the session's target"
        assert not errors
        assert not pipeline.stats.paused_for_backpressure
        assert pipeline.buffer_target_ms == 30_000
    finally:
        pipeline.close()


# Bugs caught: 0, False positives: 0
def test_pipeline_without_a_target_holds_at_ten_seconds(sample_file):
    pipeline, emitted, done, errors = _relay(sample_file, 20_000)
    try:
        deadline = time.monotonic() + 10
        while not pipeline.stats.paused_for_backpressure and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pipeline.stats.paused_for_backpressure
        assert pipeline.buffer_target_ms == DEFAULT_BUFFER_TARGET_MS
        assert not done.is_set()
        # A longer target from a later report lets it go on.
        pipeline.set_buffer_target(30_000)
        assert done.wait(30)
        assert not errors
    finally:
        pipeline.close()


async def _confirmed(port: int, sample_file: str, **cfg) -> int:
    """The target the server confirms for one session opened with ``cfg``."""
    client = RelayClient("127.0.0.1", port)
    try:
        await client.connect()
        session = await client.open_session(SessionConfig(
            path=sample_file, model="passthrough", quality_tier="lossless-ffv1",
            display_w=64, display_h=64, **cfg,
        ))
        return session.buffer_target_ms
    finally:
        await client.teardown()


# Bugs caught: 0, False positives: 0
def test_server_confirms_the_requested_target(sample_file):
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        try:
            client = RelayClient("127.0.0.1", server.port)
            await client.connect()
            try:
                session = await client.open_session(SessionConfig(
                    path=sample_file, model="passthrough", quality_tier="lossless-ffv1",
                    display_w=64, display_h=64, buffer_target_ms=45_000,
                ))
                assert session.buffer_target_ms == 45_000
                # /status reports each live session's target...
                live = next(iter(server.sessions.values()))
                assert live.status()["pipeline"]["buffer_target_ms"] == 45_000
                # ...which a report without one, or with garbage, keeps. Each
                # report carries its own depth, to know it was handled.
                for depth, fields in enumerate(({}, {"buffer_target_ms": "x"}), start=1):
                    await client._send("buffer_report", buffered_ms=depth, **fields)
                    async with asyncio.timeout(5):
                        while live.pipeline.client_buffered_ms != depth:
                            await asyncio.sleep(0.02)
                    assert live.status()["pipeline"]["buffer_target_ms"] == 45_000
                # The periodic reporter carries a new one once media attaches.
                await client.attach_media()
                client.buffer_target_ms = 90_000
                async with asyncio.timeout(5):
                    while live.status()["pipeline"]["buffer_target_ms"] != 90_000:
                        await asyncio.sleep(0.02)
            finally:
                await client.teardown()
            # No request keeps the default; there is no maximum.
            assert await _confirmed(server.port, sample_file) == DEFAULT_BUFFER_TARGET_MS
            assert await _confirmed(server.port, sample_file, buffer_target_ms=10**9) == 10**9
        finally:
            await server.stop()

    asyncio.run(scenario())


# Bugs caught: 0, False positives: 0
def test_server_rejects_a_target_that_is_not_a_number(sample_file):
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            # Past the client's own check, as another client might send it.
            with pytest.raises(RuntimeError, match="bad_message: invalid buffer_target_ms"):
                await client._request(
                    "session_opened", "open_session",
                    source={"type": "server_file", "path": "x.mkv"},
                    model="passthrough", quality_tier="lossless-ffv1",
                    display={"w": 64, "h": 64}, buffer_target_ms="lots",
                )
            assert not server.sessions or all(
                s.pipeline is None for s in server.sessions.values()
            )
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


# Bugs caught: 0, False positives: 0
def test_desktop_cache_setting_keeps_to_the_offered_choices():
    pytest.importorskip("PySide6")
    from desktop_client.settings import CACHE_DEFAULT_MIB, AppSettings, format_cache_size

    settings = AppSettings("test-buffer-target")
    settings._qs.clear()
    assert settings.cache_mib == CACHE_DEFAULT_MIB == 768
    settings.cache_mib = 5120
    assert settings.cache_mib == 5120
    with pytest.raises(ValueError):
        settings.cache_mib = 6144  # above the 5 GB limit
    settings._qs.setValue("playback/cache_mib", 1000)  # e.g. a later version's choice
    assert settings.cache_mib == CACHE_DEFAULT_MIB
    settings._qs.clear()
    assert [format_cache_size(m) for m in (512, 1024, 1536, 5120)] == ["512 MB", "1 GB", "1.5 GB", "5 GB"]


# Bugs caught: 0, False positives: 0
def test_target_is_how_long_the_cache_lasts_at_the_held_bitrate():
    from relay_client_core.buffering import cache_target_ms

    mib = 1024 * 1024
    # 25 MiB held for 4 s is 50 Mbps-ish: 768 MiB lasts 30.72 x 4 s.
    assert cache_target_ms(768 * mib, 4000, 25 * mib) == 122_880
    # Twice the bitrate in the same memory, half as long.
    assert cache_target_ms(768 * mib, 4000, 50 * mib) == 61_440
    # Under a second held, or nothing known: keep the target the session has.
    assert cache_target_ms(768 * mib, 999, 25 * mib) is None
    assert cache_target_ms(768 * mib, 4000, None) is None
    assert cache_target_ms(768 * mib, 4000, 0) is None
    assert cache_target_ms(768 * mib, 0, 25 * mib) is None
    # A full cache that holds under a second (a stream above ~2 Gbps in
    # 256 MiB) is the whole story: its length is the target.
    assert cache_target_ms(256 * mib, 860, 256 * mib) == 860
