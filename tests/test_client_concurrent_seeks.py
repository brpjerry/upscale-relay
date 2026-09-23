import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

from relay_client_core.client import RelayClient


class ControlSocket:
    closed = False

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def send_str(self, value):
        await self.sent.put(json.loads(value))

    async def close(self):
        self.closed = True

    def reply(self, epoch):
        self.incoming.put_nowait(SimpleNamespace(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps({"type": "seek_ready", "epoch": epoch}),
        ))


@pytest.mark.parametrize("replies", [(1, 2), (2, 1), (2,)])
def test_only_latest_overlapping_seek_restarts_uplink(replies):
    async def scenario():
        client = RelayClient("localhost", 8590)
        ws = client._ws = ControlSocket()
        client.track = SimpleNamespace(close=lambda: None)
        restarted = []

        async def start_uplink(**kwargs):
            restarted.append(kwargs)

        client.start_uplink = start_uplink
        client._reader_task = asyncio.create_task(client._control_reader())
        try:
            first = asyncio.create_task(client.seek(1000))
            assert (await ws.sent.get())["epoch"] == 1
            second = asyncio.create_task(client.seek(2000))
            assert (await ws.sent.get())["epoch"] == 2
            for epoch in replies:
                ws.reply(epoch)
            await asyncio.wait_for(asyncio.gather(first, second), 1)
            assert restarted == [{"from_pts": 2000, "discontinuity": True, "epoch": 2}]
            assert client._pending == {}
        finally:
            await client.close()

    asyncio.run(scenario())


def test_send_failure_does_not_leave_a_pending_request():
    async def scenario():
        client = RelayClient("localhost", 8590)

        async def fail(*args, **kwargs):
            raise ConnectionError("closed")

        client._send = fail
        try:
            with pytest.raises(ConnectionError):
                await client.seek(1000)
            assert client._pending == {}
        finally:
            await client.close()

    asyncio.run(scenario())
