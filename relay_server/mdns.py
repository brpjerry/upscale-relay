"""Optional mDNS/DNS-SD advertisement of the relay control endpoint.

Android (NSD) and any zeroconf browser can find the server as
``_upscalerelay._tcp`` without typing an IP. Advertisement is best-effort:
a missing ``zeroconf`` package or an unroutable host disables it with a log
line instead of failing server startup.
"""

from __future__ import annotations

import logging
import socket

log = logging.getLogger("relay.mdns")

SERVICE_TYPE = "_upscalerelay._tcp.local."


def txt_properties(protocol_version: int, media_port: int, server_name: str) -> dict[str, str]:
    """TXT record payload advertised alongside the control port."""
    return {
        "protocol": str(protocol_version),
        "media_port": str(media_port),
        "server": server_name,
    }


def primary_ipv4() -> str | None:
    """Best-effort local IPv4 used to reach the LAN (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routing lookup only
            address = probe.getsockname()[0]
        return None if address.startswith("127.") else address
    except OSError:
        return None


class MdnsAdvertiser:
    """Registers/unregisters the service; safe to start when zeroconf is absent."""

    def __init__(self, port: int, media_port: int, server_name: str, protocol_version: int):
        self.port = port
        self.media_port = media_port
        self.server_name = server_name
        self.protocol_version = protocol_version
        self._zeroconf = None
        self._info = None

    async def start(self) -> None:
        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
        except ImportError:
            log.info("mDNS advertisement disabled: the 'zeroconf' package is not installed")
            return
        address = primary_ipv4()
        if address is None:
            log.info("mDNS advertisement disabled: no routable IPv4 address found")
            return
        hostname = socket.gethostname().split(".")[0] or "relay"
        info = AsyncServiceInfo(
            SERVICE_TYPE,
            f"{self.server_name} on {hostname}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(address)],
            port=self.port,
            properties=txt_properties(self.protocol_version, self.media_port, self.server_name),
            server=f"{hostname}.local.",
        )
        try:
            zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await zc.async_register_service(info)
        except OSError as error:
            log.warning("mDNS advertisement disabled: %s", error)
            return
        self._zeroconf = zc
        self._info = info
        log.info("mDNS: advertising %s at %s:%d", SERVICE_TYPE, address, self.port)

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        try:
            await self._zeroconf.async_unregister_service(self._info)
            await self._zeroconf.async_close()
        except OSError:
            pass
        self._zeroconf = None
        self._info = None
