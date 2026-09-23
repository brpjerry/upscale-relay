import asyncio
from types import SimpleNamespace

import pytest

from relay_server.server import RelayServer


def test_stop_is_safe_before_start_and_repeated():
    async def scenario():
        server = RelayServer("missing-models", 0)
        await server.stop()
        await server.stop()

    asyncio.run(scenario())


def test_failed_control_bind_closes_earlier_media_listener(monkeypatch):
    async def scenario():
        occupied = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
        created = []
        original = asyncio.start_server

        async def record(*args, **kwargs):
            server = await original(*args, **kwargs)
            created.append(server)
            return server

        monkeypatch.setattr(asyncio, "start_server", record)
        server = RelayServer("missing-models", occupied.sockets[0].getsockname()[1])
        server.media_port = 0
        try:
            with pytest.raises(OSError):
                await server.start()
            assert len(created) == 1
            assert not created[0].is_serving()
            assert not created[0].sockets
            assert server._media_server is None
            assert server._runner is None
            await server.stop()
        finally:
            occupied.close()
            await occupied.wait_closed()

    asyncio.run(scenario())


def test_failed_session_cleanup_still_cleans_runner_and_websocket():
    async def scenario():
        calls = []

        async def close():
            raise RuntimeError("native owner timed out")

        async def ws_close():
            calls.append("websocket")

        async def cleanup():
            calls.append("runner")

        server = RelayServer("missing-models", 0)
        server._runner = SimpleNamespace(cleanup=cleanup)
        server.sessions = {"s": SimpleNamespace(id="s", close=close, ws=SimpleNamespace(close=ws_close))}
        with pytest.raises(RuntimeError, match="native owner"):
            await server.stop()
        assert calls == ["websocket", "runner"]

    asyncio.run(scenario())


def test_stop_closes_live_control_idle_uplink_and_unattached_peer():
    from aiohttp import ClientSession
    from relay_protocol import DIR_UPLINK, build_handshake

    async def scenario():
        server = RelayServer("missing-models", 0)
        server.media_port = 0
        await server.start()
        control_port = next(address[1] for address in server._runner.addresses if len(address) == 2)
        media_port = next(sock.getsockname()[1] for sock in server._media_server.sockets if sock.family.name == "AF_INET")
        peers = []
        async with ClientSession() as http:
            ws = await http.ws_connect(f"http://127.0.0.1:{control_port}/control")
            try:
                await ws.send_json({"type": "open_session", "quality_tier": "lossless-ffv1", "video": {
                    "codec": "h264", "width": 32, "height": 32, "time_base": [1, 1000],
                }})
                opened = await ws.receive_json()
                reader, writer = await asyncio.open_connection("127.0.0.1", media_port)
                peers.append((reader, writer))
                writer.write(build_handshake(DIR_UPLINK, opened["uplink_token"]))
                await writer.drain()
                assert await reader.readexactly(1) == b"\x00"
                peers.append(await asyncio.open_connection("127.0.0.1", media_port))
                await asyncio.wait_for(server.stop(), 2)
                assert not server._media_handlers
                assert not server.sessions
                for peer_reader, _peer_writer in peers:
                    assert await asyncio.wait_for(peer_reader.read(1), 1) == b""
            finally:
                await ws.close()
                for _peer_reader, peer_writer in peers:
                    peer_writer.close()
                    await peer_writer.wait_closed()
                await server.stop()

    asyncio.run(scenario())
