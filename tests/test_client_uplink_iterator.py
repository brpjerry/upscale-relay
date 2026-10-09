"""The client uplink pump owns its demux iterator before any worker runs.

A cancelled ``asyncio.to_thread`` keeps running. An obsolete pump whose worker
acquired its VideoTrack iterator late used to supersede the replacement seek's
iterator, and the replacement then took the early end for the end of the file.
"""

import asyncio
import threading

import av
import pytest

from relay_client_core import RelayClient
from relay_media.demux import VideoTrack
from relay_protocol import read_packet
from upscale_cli.sample import make_sample


class RecordingWriter:
    """Uplink StreamWriter stand-in that keeps the framed bytes."""

    def __init__(self, on_drain=None):
        self.data = bytearray()
        self.on_drain = on_drain

    def write(self, data):
        self.data += data

    async def drain(self):
        if self.on_drain is not None:
            await self.on_drain()

    def close(self):
        pass

    async def packets(self):
        reader = asyncio.StreamReader()
        reader.feed_data(bytes(self.data))
        reader.feed_eof()
        result = []
        while True:
            try:
                result.append(await read_packet(reader))
            except asyncio.IncompleteReadError:
                return result


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("uplink-source") / "source.mkv"
    make_sample(str(path), frames=120, width=64, height=64, fps=24)
    return str(path)


def source_packet_count(path: str) -> int:
    with av.open(path) as container:
        return sum(
            1 for p in container.demux(container.streams.video[0])
            if not (p.pts is None and p.size == 0)
        )


def test_obsolete_iterator_worker_cannot_end_the_replacement_epoch(sample_file):
    """A cancelled to_thread keeps running: its late iterator must not win."""
    expected = source_packet_count(sample_file)

    async def scenario():
        loop_thread = threading.get_ident()
        client = RelayClient("localhost", 1)
        track = client.track = VideoTrack(sample_file)
        real_packets = track.packets
        calls = 0
        entered, release, acquired = threading.Event(), threading.Event(), threading.Event()

        def packets(from_pts=None):
            nonlocal calls
            calls += 1
            if calls == 1 and threading.get_ident() != loop_thread:
                # The superseded pump's worker, delayed just before it acquires
                # its iterator until the replacement has sent its first batch.
                entered.set()
                assert release.wait(5)
                try:
                    return real_packets(from_pts)
                finally:
                    acquired.set()
            return real_packets(from_pts)

        track.packets = packets

        async def on_drain():
            if client.epoch == 1 and entered.is_set() and not release.is_set():
                release.set()
                assert await asyncio.to_thread(acquired.wait, 5)

        writer = client._uplink_writer = RecordingWriter(on_drain)
        try:
            await client.start_uplink()
            for _ in range(200):
                if entered.is_set() or writer.data:
                    break
                await asyncio.sleep(0.01)
            client.epoch = 1
            await client.start_uplink(from_pts=0, discontinuity=True, epoch=1)
            await asyncio.wait_for(client._uplink_task, 10)
            current = [p for p in await writer.packets() if p.epoch == 1]
            assert sum(1 for p in current if not p.eos) == expected
            assert [p.eos for p in current].count(True) == 1 and current[-1].eos
            assert current[0].discontinuity
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


def test_a_superseded_iterator_never_announces_the_end_of_the_source(sample_file):
    expected = source_packet_count(sample_file)

    async def scenario():
        client = RelayClient("localhost", 1)
        client.track = VideoTrack(sample_file)
        writer = client._uplink_writer = RecordingWriter()
        try:
            await client.start_uplink()
            client.epoch = 1
            # Two restarts of one epoch race, both waiting for the old pump:
            # the earlier one's iterator is superseded by the later one before
            # it reads anything, and must end without announcing EOS.
            await asyncio.gather(
                client.start_uplink(from_pts=0, discontinuity=True, epoch=1),
                client.start_uplink(from_pts=0, discontinuity=True, epoch=1),
            )
            await asyncio.wait_for(client._uplink_task, 10)
            await asyncio.sleep(0.1)  # let any orphaned pump finish its turn
            current = [p for p in await writer.packets() if p.epoch == 1]
            assert sum(1 for p in current if not p.eos) == expected
            assert [p.eos for p in current].count(True) == 1 and current[-1].eos
        finally:
            await client.close()

    asyncio.run(scenario())
