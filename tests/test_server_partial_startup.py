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
