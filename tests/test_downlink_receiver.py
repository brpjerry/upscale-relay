"""Focused coverage for the GUI-independent blocking downlink receiver."""

from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

from relay_client_core.client import RelayClient
from relay_protocol import (
    DIR_DOWNLINK,
    HANDSHAKE_LEN,
    MediaPacket,
    encode_packet,
    new_token,
    parse_handshake,
)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise EOFError
        result.extend(chunk)
    return bytes(result)


def test_blocking_receiver_handles_large_frames_and_closes_cleanly():
    payload = b"x" * (512 * 1024)
    token = new_token()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    server_done = threading.Event()

    def serve():
        conn, _ = listener.accept()
        try:
            direction, received_token = parse_handshake(_recv_exact(conn, HANDSHAKE_LEN))
            assert direction == DIR_DOWNLINK
            assert received_token == token
            conn.sendall(b"\x00")
            for pts in range(12):
                conn.sendall(encode_packet(MediaPacket(payload=payload, pts=pts)))
        finally:
            conn.close()
            listener.close()
            server_done.set()

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()

    async def scenario():
        client = RelayClient("127.0.0.1", 1)
        client.session = SimpleNamespace(media_port=port, downlink_token=token)
        client._downlink_ready = asyncio.get_running_loop().create_future()
        client._downlink_thread = threading.Thread(
            target=client._downlink_receiver_work, name="relay-downlink-test", daemon=True
        )
        client._downlink_thread.start()
        await asyncio.wait_for(client._downlink_ready, timeout=5)

        received = []
        while True:
            packet = await asyncio.wait_for(client.downlink_queue().get(), timeout=5)
            if packet is None:
                break
            received.append(packet)
        assert [packet.pts for packet in received] == list(range(12))
        assert all(packet.payload == payload for packet in received)
        stats = client.downlink_stats()
        assert stats["total_packets"] == 12
        assert stats["total_bytes"] == 12 * len(payload)
        assert stats["queue_packets"] == 0

        thread = client._downlink_thread
        await client.close()
        assert not thread.is_alive()

    asyncio.run(scenario())
    server_done.wait(timeout=5)
    server_thread.join(timeout=5)
    assert not server_thread.is_alive()
