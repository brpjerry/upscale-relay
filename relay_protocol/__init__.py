"""Shared protocol code (framing + constants) per docs/PROTOCOL.md.

Used by both the server and all clients; keep dependency-free beyond stdlib.
"""

from .buffer import (  # noqa: F401
    DEFAULT_BUFFER_TARGET_MS,
    MAX_BUFFER_TARGET_MS,
    MIN_BUFFER_TARGET_MS,
    clamp_buffer_target_ms,
    parse_buffer_target_ms,
)
from .framing import (  # noqa: F401
    DIR_DOWNLINK,
    DIR_UPLINK,
    FLAG_DISCONTINUITY,
    FLAG_EOS,
    FLAG_KEYFRAME,
    HANDSHAKE_LEN,
    MAGIC,
    MAX_PAYLOAD_BYTES,
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
