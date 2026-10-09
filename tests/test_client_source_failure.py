"""A failing local/SMB source read ends the relay session's media visibly."""

import asyncio
import threading
from types import SimpleNamespace

from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig, cli
from relay_client_core import client as client_module
from relay_media.demux import VideoTrack
from relay_server.server import RelayServer
from test_client_uplink_iterator import RecordingWriter, sample_file  # noqa: F401


class FailingTrack(VideoTrack):
    """A mounted source whose reads start failing partway through (an SMB drop)."""

    fail_after = 40
    error = OSError("SMB source read failed")

    def packets(self, from_pts=None):
        iterator = super().packets(from_pts)

        def reads():
            for count, info in enumerate(iterator):
                if count == self.fail_after:
                    raise self.error
                yield info

        return reads()


async def downlink_end(client: RelayClient):
    """Consume the downlink until it ends; returns the final item (None or EOS)."""
    queue = client.downlink_queue()
    while True:
        pkt = await queue.get()
        client.buffered_ms = 0
        if pkt is None or pkt.eos:
            return pkt


def test_source_read_failure_ends_the_downlink_with_the_error(sample_file, monkeypatch, tmp_path):
    monkeypatch.setattr(client_module, "VideoTrack", FailingTrack)

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(
                path=sample_file, model="passthrough", quality_tier="lossless-ffv1",
                display_w=64, display_h=64,
            ))
            await client.attach_media()
            await client.start_uplink()
            await client.play()
            # Before the fix the pump died silently and this waited forever.
            end = await asyncio.wait_for(downlink_end(client), 15)
            assert end is None  # the source never reached its end
            assert client.source_error == "Could not read the source file: SMB source read failed"
        finally:
            # The session is still owned by its caller: teardown stays confirmed.
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_cli_reports_a_source_read_failure_and_exits_nonzero(sample_file, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(client_module, "VideoTrack", FailingTrack)

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair())
        await server.start()
        try:
            args = SimpleNamespace(
                host="127.0.0.1", port=server.port, file=sample_file, display="64x64",
                model="passthrough", tier="lossless-ffv1", fit_mode="fit",
                resize_algorithm="server-default", decode=False, mpv=False, save=None,
            )
            return await asyncio.wait_for(cli.run(args), 15)
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == 1
    assert "Could not read the source file: SMB source read failed" in capsys.readouterr().err


class ScriptedTrack:
    """A VideoTrack stand-in whose iterators follow a per-call script."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)

    def packets(self, from_pts=None):
        return self.scripts.pop(0)()

    def media_packet(self, info, epoch, discontinuity=False):
        return VideoTrack.media_packet(self, info, epoch, discontinuity)

    def close(self):
        pass


def packets_then(count, error):
    def script():
        for pts in range(count):
            yield SimpleNamespace(payload=b"x", pts=pts, dts=pts, keyframe=True)
        raise error
    return script


async def queue_closed(client) -> bool:
    try:
        return await asyncio.wait_for(client.downlink_queue().get(), 0.2) is None
    except asyncio.TimeoutError:
        return False


def test_a_reset_reported_by_the_source_is_a_source_failure():
    # ECONNRESET from an SMB read (PyAV raises a ConnectionResetError subclass)
    # is not the server closing the uplink socket.
    async def scenario():
        client = RelayClient("localhost", 1)
        client.track = ScriptedTrack(packets_then(1, ConnectionResetError(104, "Connection reset by peer")))
        client._uplink_writer = RecordingWriter()
        try:
            await client.start_uplink()
            await asyncio.wait_for(client._uplink_task, 5)
            assert client.source_error == (
                "Could not read the source file: [Errno 104] Connection reset by peer")
            assert await queue_closed(client)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_the_server_closing_the_uplink_is_not_a_source_failure():
    async def scenario():
        client = RelayClient("localhost", 1)
        # One full batch is read and sent; the pump must stop at the closed
        # socket instead of reading (and failing) any further.
        client.track = ScriptedTrack(packets_then(client_module._UPLINK_BATCH, AssertionError("read on")))

        async def reset():
            raise ConnectionResetError("Connection lost")

        client._uplink_writer = RecordingWriter(on_drain=reset)
        try:
            await client.start_uplink()
            await asyncio.wait_for(client._uplink_task, 5)
            assert client.source_error is None
            assert not await queue_closed(client)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_a_read_failing_after_its_seek_was_superseded_is_not_reported():
    started, release, failed = threading.Event(), threading.Event(), threading.Event()

    def blocked_read():
        started.set()
        assert release.wait(5)
        failed.set()
        raise OSError("read of the abandoned position failed")
        yield  # pragma: no cover - makes this a generator

    def exhausted():
        return iter(())

    async def scenario():
        client = RelayClient("localhost", 1)
        client.track = ScriptedTrack(blocked_read, exhausted)
        writer = client._uplink_writer = RecordingWriter()
        try:
            await client.start_uplink()
            assert await asyncio.to_thread(started.wait, 5)
            # A seek cancels the pump while its worker is still inside the read.
            client.epoch = 1
            await client.start_uplink(from_pts=0, discontinuity=True, epoch=1)
            await asyncio.wait_for(client._uplink_task, 5)
            release.set()
            assert await asyncio.to_thread(failed.wait, 5)
            await asyncio.sleep(0.05)
            assert client.source_error is None
            assert not await queue_closed(client)
            sent = await writer.packets()
            assert [(p.epoch, p.eos) for p in sent] == [(1, True)]
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())
