"""open_session keepalive: slow pipeline builds must not time out clients.

A first-use TensorRT engine build blocks pipeline construction for minutes.
The server ticks session_progress while it runs; the client treats each tick
as activity and only fails after a silent inactivity window
(docs/PROTOCOL.md session_progress).
"""

import asyncio
import socket
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import relay_client_core.client as client_mod
import relay_server.session as session_mod
from relay_client_core import RelayClient, SessionConfig
from relay_server.server import RelayServer


_next_port = [0]


def free_port_pair() -> int:
    """Walk a private, non-ephemeral range for an unused adjacent pair."""
    import random

    if _next_port[0] == 0:
        _next_port[0] = random.randrange(24000, 39000, 2)
    for _ in range(200):
        candidate = _next_port[0]
        _next_port[0] += 2
        try:
            with socket.socket() as control, socket.socket() as media:
                control.bind(("127.0.0.1", candidate))
                media.bind(("127.0.0.1", candidate + 1))
            return candidate
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture(scope="module")
def sample_file() -> str:
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    return str(sample)


class SlowPipeline:
    """Pipeline stand-in: a long blocking __init__ (the engine build), then
    just enough surface for session_opened and teardown."""

    build_seconds = 1.2

    downlink_container = "matroska"
    downlink_codec = "hevc"
    downlink_extradata_b64 = None
    out_w = 320
    out_h = 180
    fit_mode = "fit"
    resize_algorithm = "lanczos"

    def __init__(self, *args, **kwargs):
        self.playing = False
        time.sleep(self.build_seconds)

    def note_buffer_report(self, ms: int) -> None:
        pass

    def close(self) -> None:
        pass


def test_progress_keepalives_hold_a_slow_open_alive(sample_file, monkeypatch):
    monkeypatch.setattr(session_mod, "Pipeline", SlowPipeline)
    monkeypatch.setattr(session_mod, "PROGRESS_INITIAL_DELAY_S", 0.1)
    monkeypatch.setattr(session_mod, "PROGRESS_INTERVAL_S", 0.1)
    # Far shorter than the 1.2 s build: only the keepalives can save it.
    monkeypatch.setattr(client_mod, "OPEN_SESSION_TIMEOUT_S", 0.5)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        progress: list[dict] = []
        client.on_progress = progress.append
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path=sample_file, model="passthrough",
                display_w=320, display_h=180,
            ))
            assert session.downlink_codec == "hevc"
            assert progress, "expected session_progress keepalives"
            assert progress[0]["stage"] == "pipeline_init"
            assert "TensorRT" in progress[0]["message"]
            assert progress[-1]["elapsed_s"] >= 0
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_ws_pings_answered_during_slow_open(sample_file, monkeypatch):
    """The control loop must keep servicing WS ping/pong during a build.

    OkHttp (Android) pings every 10 s and drops the socket without a pong;
    aiohttp only auto-answers pings inside receive(), so handle_open must not
    block the receive loop. An aggressive client heartbeat reproduces the
    Android failure in miniature.
    """
    import json

    import aiohttp

    from relay_media import VideoTrack

    monkeypatch.setattr(session_mod, "Pipeline", SlowPipeline)
    monkeypatch.setattr(session_mod, "PROGRESS_INITIAL_DELAY_S", 0.1)
    monkeypatch.setattr(session_mod, "PROGRESS_INTERVAL_S", 0.1)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        track = VideoTrack(sample_file)
        video = track.open_session_video_dict()
        track.close()
        opened = None
        try:
            async with aiohttp.ClientSession() as http:
                # ping every 0.3 s, pong required within 0.15 s — far tighter
                # than the 1.2 s build. Fails if the server's receive loop is
                # blocked by handle_open.
                async with http.ws_connect(
                    f"http://127.0.0.1:{server.port}/control", heartbeat=0.3,
                ) as ws:
                    await ws.send_json({
                        "type": "hello", "protocol_version": 1,
                        "client_name": "ping-test", "display": {"w": 0, "h": 0},
                    })
                    async for raw in ws:
                        msg = json.loads(raw.data)
                        if msg["type"] == "capabilities":
                            await ws.send_json({
                                "type": "open_session", "source": "uplink",
                                "file": {"name": "ping-test.mkv"}, "video": video,
                                "model": "passthrough",
                                "quality_tier": "lossless-hevc",
                                "display": {"w": 320, "h": 180},
                            })
                        elif msg["type"] == "session_opened":
                            opened = msg
                            break
        finally:
            await server.stop()
        assert opened is not None, "control WS dropped during the slow open"

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_silent_open_still_times_out(sample_file, monkeypatch):
    monkeypatch.setattr(session_mod, "Pipeline", SlowPipeline)
    # Keepalives suppressed: the initial delay never elapses during the build.
    monkeypatch.setattr(session_mod, "PROGRESS_INITIAL_DELAY_S", 60.0)
    monkeypatch.setattr(client_mod, "OPEN_SESSION_TIMEOUT_S", 0.4)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await client.open_session(SessionConfig(
                    path=sample_file, model="passthrough",
                    display_w=320, display_h=180,
                ))
            # Let the server-side build finish before stopping the server so
            # its open handler isn't cancelled mid-thread.
            await asyncio.sleep(SlowPipeline.build_seconds + 0.3)
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
