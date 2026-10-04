"""Server shutdown must not wait on clients that are merely connected."""

from __future__ import annotations

import asyncio
import time

import aiohttp

from ports import free_port_pair
from relay_server.server import RelayServer


def test_stop_does_not_wait_for_an_idle_control_connection(tmp_path):
    # A client sitting in the library browser holds a control WebSocket but
    # owns no Session. aiohttp's runner waits out its shutdown timeout (60 s)
    # for such a handler unless the server closes the socket itself, which
    # made Quit in the tray GUI look like a hang.
    port = free_port_pair()

    async def scenario() -> float:
        server = RelayServer(str(tmp_path), port, mdns=False)
        await server.start()
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(f"http://127.0.0.1:{port}/control")
            await asyncio.sleep(0.1)  # let the handler reach its receive loop
            started = time.perf_counter()
            await asyncio.wait_for(server.stop(), timeout=90)
            elapsed = time.perf_counter() - started
            await ws.close()
        return elapsed

    assert asyncio.run(scenario()) < 5
