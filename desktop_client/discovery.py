"""Asynchronous nearby-server discovery; manual address entry remains authoritative."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from relay_protocol.discovery import SERVICE_TYPE


@dataclass(frozen=True)
class NearbyServer:
    name: str
    host: str
    port: int

    @property
    def address(self):
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


class ServerDiscovery:
    def __init__(self, changed, failed=lambda message: None):
        self.changed, self.failed = changed, failed
        self.services = {}
        self._versions = {}
        self._tasks = set()
        self._zc = self._browser = None
        self._closed = False

    async def start(self):
        if self._closed:
            return
        try:
            self._zc = AsyncZeroconf()
            self._browser = AsyncServiceBrowser(self._zc.zeroconf, SERVICE_TYPE, handlers=[self._change])
        except Exception as err:
            self.failed(str(err))
            await self.close()

    def _change(self, zeroconf, service_type, name, state_change):
        if self._closed:
            return
        version = self._versions[name] = self._versions.get(name, 0) + 1
        if state_change is ServiceStateChange.Removed:
            self.services.pop(name, None)
            self.changed(dict(self.services))
            return
        task = asyncio.create_task(self._resolve(name, version))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, name, version):
        try:
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if not await info.async_request(self._zc.zeroconf, 2500):
                raise TimeoutError(f"Could not resolve nearby server {name}")
            addresses = info.parsed_scoped_addresses()
            if not addresses or not 1 <= info.port <= 65534:
                raise ValueError(f"Invalid nearby server address for {name}")
            host = next((v for v in addresses if ":" not in v), addresses[0])
            label = info.properties.get(b"server", b"").decode("utf-8", "replace") or name.removesuffix("." + SERVICE_TYPE)
            if not self._closed and version == self._versions.get(name):
                self.services[name] = NearbyServer(label, host, info.port)
                self.changed(dict(self.services))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            if not self._closed and version == self._versions.get(name):
                self.services.pop(name, None)
                self.changed(dict(self.services))
                self.failed(str(err))

    async def close(self):
        self._closed = True
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._zc:
            await self._zc.async_close()
            self._zc = None
        self.services.clear()
