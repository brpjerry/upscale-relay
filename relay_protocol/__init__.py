"""Shared protocol code (framing + constants) per docs/PROTOCOL.md.

Used by both the server and all clients; keep dependency-free beyond stdlib.
"""

from .framing import (  # noqa: F401
    DIR_DOWNLINK,
    DIR_UPLINK,
    FLAG_DISCONTINUITY,
    FLAG_EOS,
    FLAG_KEYFRAME,
    HANDSHAKE_LEN,
    MAGIC,
    NO_TS,
    PROTOCOL_VERSION,
    MediaPacket,
    build_handshake,
    encode_packet,
    new_token,
    parse_handshake,
    read_exact,
    read_packet,
    read_packet_sync,
)
