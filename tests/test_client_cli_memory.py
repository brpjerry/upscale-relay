import asyncio
from fractions import Fraction
from types import SimpleNamespace
import tracemalloc

import pytest

from relay_client_core import cli
from relay_protocol import FLAG_DISCONTINUITY, FLAG_EOS, MediaPacket


class Client:
    epoch = 0

    def __init__(self, packets):
        self.packets = iter(packets)
        self.track = SimpleNamespace(time_base=Fraction(1, 30))
        self.closed = False

    async def connect(self):
        return {"server_name": "test", "models": []}

    async def open_session(self, config):
        return SimpleNamespace(session_id="test", downlink_codec="ffv1",
                               downlink_width=32, downlink_height=32)

    async def attach_media(self):
        pass

    async def start_uplink(self):
        pass

    async def play(self):
        pass

    async def teardown(self):
        self.closed = True

    def downlink_queue(self):
        return self

    async def get(self):
        return next(self.packets)


def arguments(**extra):
    return SimpleNamespace(**dict(host="localhost", port=1, file="unused",
        display="32x32", model="passthrough", tier="lossless-ffv1",
        fit_mode="fit", resize_algorithm="server-default", mpv=False,
        save=None, **extra))


def test_playback_memory_does_not_grow_with_received_video(monkeypatch):
    def packets():
        for i in range(64):
            yield MediaPacket(b"x" * 1024 * 1024, pts=i)
        yield MediaPacket(b"", flags=FLAG_EOS)

    client = Client(packets())
    monkeypatch.setattr(cli, "RelayClient", lambda *args: client)
    tracemalloc.start()
    try:
        assert asyncio.run(cli.run(arguments(decode=False))) == 0
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 8 * 1024 * 1024
    finally:
        tracemalloc.stop()
    assert client.closed


def test_decode_spool_keeps_only_latest_epoch_and_is_closed(monkeypatch):
    packets = [MediaPacket(b"old", pts=0),
               MediaPacket(b"new", flags=FLAG_DISCONTINUITY, epoch=1, pts=1),
               MediaPacket(b"data", epoch=1, pts=2),
               MediaPacket(b"", flags=FLAG_EOS, epoch=1)]
    client = Client(packets)
    monkeypatch.setattr(cli, "RelayClient", lambda *args: client)
    captured = []

    class Container:
        streams = SimpleNamespace(video=[object()])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def decode(self, stream):
            return iter([object()])

    def decode(source):
        captured.append(source)
        assert source.read() == b"newdata"
        return Container()

    monkeypatch.setattr(cli.av, "open", decode)
    assert asyncio.run(cli.run(arguments(decode=True))) == 0
    assert captured[0].closed
    assert client.closed


def test_consumer_failure_closes_client(monkeypatch):
    client = Client([])  # unexpected iterator exhaustion in the receiver
    monkeypatch.setattr(cli, "RelayClient", lambda *args: client)
    with pytest.raises(RuntimeError, match="StopIteration"):
        asyncio.run(cli.run(arguments(decode=False)))
    assert client.closed
