"""The control connection notices a vanished peer instead of looking alive."""
import asyncio
import json

from aiohttp import web

import relay_client_core.client as client_module
from relay_client_core import RelayClient
from ports import free_port_pair


async def _serve(handler):
    app = web.Application()
    app.router.add_get("/control", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port_pair()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


async def _answer_hello(ws):
    async for raw in ws:
        if json.loads(raw.data).get("type") == "hello":
            await ws.send_str(json.dumps({"type": "capabilities", "models": []}))
            return


def test_silent_peer_is_detected_by_the_heartbeat(monkeypatch):
    """A peer that stops answering (suspend, dropped Wi-Fi) sends no FIN."""
    monkeypatch.setattr(client_module, "CONTROL_HEARTBEAT_S", 0.4)
    release = asyncio.Event()

    async def handler(request):
        # autoping off: after the handshake this server never answers a ping.
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        await _answer_hello(ws)
        await release.wait()
        return ws

    async def scenario():
        runner, port = await _serve(handler)
        client = RelayClient("127.0.0.1", port)
        lost = []
        try:
            await client.connect()
            client.on_disconnected = lambda: lost.append(True)
            assert client.connected
            async with asyncio.timeout(5):
                while not lost:
                    await asyncio.sleep(0.05)
            assert not client.connected
            assert lost == [True]
        finally:
            release.set()
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())


def test_deliberate_close_does_not_report_a_lost_connection():
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await _answer_hello(ws)
        async for _ in ws:
            pass
        return ws

    async def scenario():
        runner, port = await _serve(handler)
        client = RelayClient("127.0.0.1", port)
        lost = []
        try:
            await client.connect()
            client.on_disconnected = lambda: lost.append(True)
            await client.close()
            await asyncio.sleep(0.1)
            assert not client.connected
            assert lost == []
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


def test_server_side_close_reports_a_lost_connection():
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await _answer_hello(ws)
        await ws.close()
        return ws

    async def scenario():
        runner, port = await _serve(handler)
        client = RelayClient("127.0.0.1", port)
        lost = asyncio.Event()
        try:
            client.on_disconnected = lost.set
            await client.connect()
            async with asyncio.timeout(5):
                await lost.wait()
            assert not client.connected
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(scenario())
