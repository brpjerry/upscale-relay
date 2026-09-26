import asyncio
from types import SimpleNamespace

from zeroconf import ServiceStateChange

import desktop_client.discovery as discovery


def test_add_update_remove_and_stale_resolution(monkeypatch):
    async def scenario():
        results, failures = [], []
        delayed = asyncio.Event()
        count = 0
        class Info:
            def __init__(self, kind, name):
                nonlocal count
                count += 1
                self.number = count
                self.port = 8590
                self.properties = {b"server": b"Friendly name"}
            async def async_request(self, zc, timeout):
                if self.number == 1:
                    await delayed.wait()
                return True
            def parsed_scoped_addresses(self):
                return [f"192.0.2.{self.number}"]
        monkeypatch.setattr(discovery, "AsyncServiceInfo", Info)
        browser = discovery.ServerDiscovery(results.append, failures.append)
        browser._zc = SimpleNamespace(zeroconf=None)
        browser._change(None, discovery.SERVICE_TYPE, "server", ServiceStateChange.Added)
        await asyncio.sleep(0)
        browser._change(None, discovery.SERVICE_TYPE, "server", ServiceStateChange.Updated)
        await asyncio.sleep(0)
        assert browser.services["server"].host == "192.0.2.2"
        delayed.set()
        await asyncio.sleep(0)
        assert browser.services["server"].host == "192.0.2.2"
        browser._change(None, discovery.SERVICE_TYPE, "server", ServiceStateChange.Removed)
        assert browser.services == {}
        assert results[-1] == {}
        assert not failures
    asyncio.run(scenario())


def test_failed_resolution_and_shutdown_cancel_pending_tasks(monkeypatch):
    async def scenario():
        class Info:
            def __init__(self, *args):
                pass
            async def async_request(self, *args):
                return False
        monkeypatch.setattr(discovery, "AsyncServiceInfo", Info)
        failures = []
        browser = discovery.ServerDiscovery(lambda _: None, failures.append)
        closed = []
        async def close():
            closed.append(True)
        browser._zc = SimpleNamespace(zeroconf=None, async_close=close)
        browser._change(None, discovery.SERVICE_TYPE, "server", ServiceStateChange.Added)
        await asyncio.sleep(0)
        assert failures and not browser.services
        pending = asyncio.create_task(asyncio.Event().wait())
        browser._tasks.add(pending)
        await browser.close()
        assert pending.cancelled()
        assert closed == [True]
        browser._change(None, discovery.SERVICE_TYPE, "late", ServiceStateChange.Added)
        assert browser.services == {}
    asyncio.run(scenario())
