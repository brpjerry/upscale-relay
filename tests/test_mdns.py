"""mDNS advertisement payload and address selection."""

from relay_server.mdns import SERVICE_TYPE, primary_ipv4, txt_properties


def test_txt_properties_are_strings():
    props = txt_properties(protocol_version=1, media_port=8591, server_name="upscale-relay")
    assert props == {"protocol": "1", "media_port": "8591", "server": "upscale-relay"}
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in props.items())


def test_service_type_is_dns_sd_shaped():
    assert SERVICE_TYPE.startswith("_upscalerelay._tcp.")
    assert SERVICE_TYPE.endswith(".local.")


def test_primary_ipv4_is_none_or_non_loopback():
    address = primary_ipv4()
    assert address is None or not address.startswith("127.")


import asyncio

import pytest
import zeroconf.asyncio

from relay_server import mdns


class _TrackingZeroconf:
    """AsyncZeroconf stand-in recording what each instance was asked to do."""

    instances: list["_TrackingZeroconf"] = []
    register_error: BaseException | None = None
    register_gate: asyncio.Event | None = None
    unregister_error: BaseException | None = None

    def __init__(self, **_kwargs):
        self.registered = []
        self.unregistered = []
        self.closed = 0
        type(self).instances.append(self)

    async def async_register_service(self, info):
        if self.register_gate is not None:
            await self.register_gate.wait()
        if self.register_error is not None:
            raise self.register_error
        self.registered.append(info)

    async def async_unregister_service(self, info):
        if self.unregister_error is not None:
            raise self.unregister_error
        self.unregistered.append(info)

    async def async_close(self):
        self.closed += 1


@pytest.fixture
def tracking_zeroconf(monkeypatch):
    class Tracking(_TrackingZeroconf):
        instances = []

    monkeypatch.setattr(zeroconf.asyncio, "AsyncZeroconf", Tracking)
    monkeypatch.setattr(mdns, "primary_ipv4", lambda: "192.0.2.10")
    return Tracking


def _advertiser():
    return mdns.MdnsAdvertiser(8590, 8591, "upscale-relay", 1)


def test_failed_registration_closes_its_zeroconf(tracking_zeroconf):
    tracking_zeroconf.register_error = OSError("interface disappeared")

    async def run():
        advertiser = _advertiser()
        await advertiser.start()  # best-effort: logs instead of raising
        await advertiser.stop()

    asyncio.run(run())
    assert [zc.closed for zc in tracking_zeroconf.instances] == [1]


def test_cancelled_registration_is_closed_by_stop(tracking_zeroconf):
    tracking_zeroconf.register_gate = asyncio.Event()  # never set: hang in register

    async def run():
        advertiser = _advertiser()
        starting = asyncio.create_task(advertiser.start())
        while not tracking_zeroconf.instances:
            await asyncio.sleep(0)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        await advertiser.stop()  # the server's startup rollback path

    asyncio.run(run())
    assert [zc.closed for zc in tracking_zeroconf.instances] == [1]


def test_stop_closes_even_when_unregistration_fails(tracking_zeroconf):
    tracking_zeroconf.unregister_error = OSError("interface disappeared")

    async def run():
        advertiser = _advertiser()
        await advertiser.start()
        await advertiser.stop()
        await advertiser.stop()  # idempotent: nothing left to close

    asyncio.run(run())
    [zc] = tracking_zeroconf.instances
    assert len(zc.registered) == 1
    assert zc.closed == 1


def test_successful_advertisement_unregisters_then_closes(tracking_zeroconf):
    async def run():
        advertiser = _advertiser()
        await advertiser.start()
        await advertiser.stop()

    asyncio.run(run())
    [zc] = tracking_zeroconf.instances
    assert zc.unregistered == zc.registered and len(zc.registered) == 1
    assert zc.closed == 1
