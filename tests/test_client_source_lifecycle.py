import asyncio
import threading
from types import SimpleNamespace

import pytest

from relay_client_core import client as module


def test_cancelled_slow_open_releases_unpublished_source(monkeypatch):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def open_source(path):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(close=closed.set), {}, None, []

    monkeypatch.setattr(module, "_open_local_source", open_source)

    async def scenario():
        client = module.RelayClient("localhost", 1)
        task = asyncio.create_task(client.open_session(module.SessionConfig("slow.mkv")))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            # The event loop remains responsive while libav still owns the file.
            await asyncio.sleep(0)
            assert not closed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()
            assert client.track is None
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


def test_close_during_source_open_cannot_publish_the_late_track(monkeypatch):
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def open_source(path):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(close=closed.set), {}, None, []

    monkeypatch.setattr(module, "_open_local_source", open_source)

    async def scenario():
        client = module.RelayClient("localhost", 1)
        task = asyncio.create_task(client.open_session(module.SessionConfig("slow.mkv")))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await client.close()
            release.set()
            with pytest.raises(ConnectionError, match="closed while opening"):
                await task
            assert closed.is_set()
            assert client.track is None
        finally:
            release.set()
            await client.close()

    asyncio.run(scenario())


def test_concurrent_close_releases_source_once_off_the_event_loop():
    async def scenario():
        loop_thread = threading.get_ident()
        closed_on = []
        client = module.RelayClient("localhost", 1)
        client.track = SimpleNamespace(close=lambda: closed_on.append(threading.get_ident()))
        await asyncio.gather(client.close(), client.close())
        assert len(closed_on) == 1
        assert closed_on[0] != loop_thread
        assert client.track is None

    asyncio.run(scenario())
