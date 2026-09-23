import asyncio
import struct

import pytest

import relay_protocol.framing as framing


class SocketBytes:
    def __init__(self, data):
        self.data = bytearray(data)
        self.read_sizes = []

    def recv_into(self, view):
        self.read_sizes.append(len(view))
        count = min(len(view), len(self.data))
        view[:count] = self.data[:count]
        del self.data[:count]
        return count


@pytest.mark.parametrize("length", [framing.MAX_PAYLOAD_BYTES + 1, 0xffffffff])
def test_oversized_header_is_rejected_without_reading_a_body(length):
    header = struct.pack("<IBIqq", length, 0, 0, 0, 0)
    sock = SocketBytes(header)
    with pytest.raises(ValueError, match="payload exceeds"):
        framing.read_packet_sync(sock)
    assert sock.read_sizes == [framing.HEADER_LEN]

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(header)
        reader.feed_eof()
        with pytest.raises(ValueError, match="payload exceeds"):
            await framing.read_packet(reader)

    asyncio.run(scenario())


def test_limit_boundary_and_timestamps_roundtrip(monkeypatch):
    monkeypatch.setattr(framing, "MAX_PAYLOAD_BYTES", 8)
    packet = framing.MediaPacket(b"12345678", flags=3, epoch=7, pts=900, dts=800)
    encoded = framing.encode_packet(packet)
    assert framing.read_packet_sync(SocketBytes(encoded)) == packet
    with pytest.raises(ValueError, match="payload exceeds"):
        framing.encode_packet(framing.MediaPacket(b"123456789"))

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(encoded)
        assert await framing.read_packet(reader) == packet

    asyncio.run(scenario())
