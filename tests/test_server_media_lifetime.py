import asyncio
import json
import socket

import pytest

from relay_protocol import DIR_DOWNLINK, DIR_UPLINK, MediaPacket, build_handshake
from relay_server.server import RelayServer
from relay_server.session import Session


class _Ws:
    closed = False

    def __init__(self):
        self.messages = []

    async def send_str(self, message):
        self.messages.append(json.loads(message))


async def _server():
    relay = RelayServer("missing-models", 0)
    ws = _Ws()
    session = Session(ws, {})
    relay.sessions = {session.uplink_token: session, session.downlink_token: session, session.id: session}
    listener = await asyncio.start_server(relay.handle_media, "127.0.0.1", 0)
    return relay, session, ws, listener


async def _attach(listener, session, direction):
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
    token = session.uplink_token if direction == DIR_UPLINK else session.downlink_token
    writer.write(build_handshake(direction, token))
    await writer.drain()
    return reader, writer, await asyncio.wait_for(reader.readexactly(1), 1)


def test_closed_ack_releases_idle_media_sockets_and_payloads():
    async def scenario():
        relay, session, ws, listener = await _server()
        clients = []
        try:
            for direction in (DIR_UPLINK, DIR_DOWNLINK):
                reader, writer, result = await _attach(listener, session, direction)
                clients.append((reader, writer))
                assert result == b"\x00"
            assert await relay._close_control_session(session, ws, acknowledge=True)
            assert ws.messages[-1] == {"type": "closed"}
            assert not relay.sessions
            assert not session.uplink_attached and not session.downlink_attached
            assert session.down_q.payload_bytes == 0
            for reader, _writer in clients:
                assert await asyncio.wait_for(reader.read(1), 1) == b""
        finally:
            await session.close()
            for _reader, writer in clients:
                writer.close()
                await writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", [DIR_UPLINK, DIR_DOWNLINK])
def test_duplicate_attachment_is_rejected_without_replacing_original(direction):
    async def scenario():
        _relay, session, _ws, listener = await _server()
        clients = []
        try:
            for expected in (b"\x00", b"\x01"):
                reader, writer, result = await _attach(listener, session, direction)
                clients.append((reader, writer))
                assert result == expected
            assert len(session._media_connections) == 1
            assert await asyncio.wait_for(clients[1][0].read(1), 1) == b""
        finally:
            await session.close()
            for _reader, writer in clients:
                writer.close()
                await writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    asyncio.run(scenario())


def test_close_aborts_downlink_blocked_on_nonreading_peer():
    async def scenario():
        _relay, session, _ws, listener = await _server()
        reader, writer, result = await _attach(listener, session, DIR_DOWNLINK)
        try:
            assert result == b"\x00"
            sender, task = session._media_connections[DIR_DOWNLINK]
            sender.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            for _ in range(16):
                session.down_q.put_nowait(MediaPacket(payload=b"x" * (1024 * 1024)))
            for _ in range(100):
                if sender.transport.get_write_buffer_size():
                    break
                await asyncio.sleep(0.01)
            assert sender.transport.get_write_buffer_size() > 0
            await asyncio.wait_for(session.close(), 1)
            assert task.done()
            assert session.down_q.payload_bytes == 0
        finally:
            await session.close()
            writer.close()
            await writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    asyncio.run(scenario())


def test_media_handler_can_initiate_close_without_awaiting_itself():
    async def scenario():
        session = Session(_Ws(), {})
        finished = asyncio.Event()

        async def handler(_reader, writer):
            assert session.register_media(DIR_UPLINK, writer)
            await session.close()
            session.unregister_media(DIR_UPLINK, writer)
            finished.set()

        listener = await asyncio.start_server(handler, "127.0.0.1", 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
        try:
            await asyncio.wait_for(finished.wait(), 1)
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    asyncio.run(scenario())
