"""Media packet framing and handshake per docs/PROTOCOL.md §3."""

from __future__ import annotations

import asyncio
import secrets
import struct
from dataclasses import dataclass

PROTOCOL_VERSION = 1

MAGIC = b"UPRLY1"
DIR_UPLINK = 0x01
DIR_DOWNLINK = 0x02

TOKEN_HEX_LEN = 34  # 17 random bytes, hex-encoded
HANDSHAKE_LEN = len(MAGIC) + 1 + TOKEN_HEX_LEN  # 41

FLAG_KEYFRAME = 0x01
FLAG_DISCONTINUITY = 0x02
FLAG_EOS = 0x04

NO_TS = -(2**63)  # INT64_MIN

# payload_len, flags, epoch, pts, dts
_HEADER = struct.Struct("<IBIqq")
HEADER_LEN = _HEADER.size  # 25


@dataclass(slots=True)
class MediaPacket:
    payload: bytes
    flags: int = 0
    epoch: int = 0
    pts: int = NO_TS
    dts: int = NO_TS

    @property
    def keyframe(self) -> bool:
        return bool(self.flags & FLAG_KEYFRAME)

    @property
    def discontinuity(self) -> bool:
        return bool(self.flags & FLAG_DISCONTINUITY)

    @property
    def eos(self) -> bool:
        return bool(self.flags & FLAG_EOS)


def encode_packet(pkt: MediaPacket) -> bytes:
    return _HEADER.pack(len(pkt.payload), pkt.flags, pkt.epoch, pkt.pts, pkt.dts) + pkt.payload


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    data = await reader.readexactly(n)
    return data


async def read_packet(reader: asyncio.StreamReader) -> MediaPacket:
    """Read one framed packet; raises asyncio.IncompleteReadError on EOF."""
    header = await reader.readexactly(HEADER_LEN)
    payload_len, flags, epoch, pts, dts = _HEADER.unpack(header)
    payload = await reader.readexactly(payload_len) if payload_len else b""
    return MediaPacket(payload=payload, flags=flags, epoch=epoch, pts=pts, dts=dts)


def _recv_exact(sock, n: int) -> bytes:
    """Blocking socket equivalent of StreamReader.readexactly()."""
    data = bytearray(n)
    view = memoryview(data)
    received = 0
    while received < n:
        count = sock.recv_into(view[received:])
        if count == 0:
            raise EOFError("socket closed during framed packet")
        received += count
    return bytes(data)


def read_packet_sync(sock) -> MediaPacket:
    """Read one framed packet from a blocking socket.

    The desktop downlink uses this off the qasync/Qt loop so large lossless
    payloads are not limited by selector callbacks paced by GUI rendering.
    """
    header = _recv_exact(sock, HEADER_LEN)
    payload_len, flags, epoch, pts, dts = _HEADER.unpack(header)
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return MediaPacket(payload=payload, flags=flags, epoch=epoch, pts=pts, dts=dts)


def new_token() -> str:
    return secrets.token_hex(17)  # 34 hex chars


def build_handshake(direction: int, token: str) -> bytes:
    raw = token.encode("ascii")
    if len(raw) != TOKEN_HEX_LEN:
        raise ValueError("token must be 34 hex chars")
    return MAGIC + bytes([direction]) + raw


def parse_handshake(data: bytes) -> tuple[int, str]:
    """Returns (direction, token); raises ValueError on garbage."""
    if len(data) != HANDSHAKE_LEN or not data.startswith(MAGIC):
        raise ValueError("bad handshake")
    direction = data[len(MAGIC)]
    if direction not in (DIR_UPLINK, DIR_DOWNLINK):
        raise ValueError("bad direction")
    token = data[len(MAGIC) + 1 :].decode("ascii")
    return direction, token
