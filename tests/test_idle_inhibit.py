"""Desktop inhibition follows playback without leaking delayed requests."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("dbus_next")
from dbus_next import MessageType
import dbus_next.aio

from desktop_client.idle_inhibit import IdleInhibitor


class FakeBus:
    def __init__(self, *, delayed=False, error=False):
        self.calls = []
        self.disconnected = False
        self.delayed = delayed
        self.error = error
        self.reply = asyncio.Event()
        self.closed = asyncio.Event()

    async def connect(self):
        return self

    async def call(self, message):
        self.calls.append(message)
        if self.delayed:
            await self.reply.wait()
        return SimpleNamespace(
            message_type=MessageType.ERROR if self.error else MessageType.METHOD_RETURN,
            error_name="org.freedesktop.DBus.Error.ServiceUnknown", body=[7],
        )

    def disconnect(self):
        self.disconnected = True
        self.closed.set()

    async def wait_for_disconnect(self):
        await self.closed.wait()


@pytest.mark.parametrize("delayed", [False, True])
def test_pause_releases_inhibitor_even_before_reply_and_resume_gets_new_connection(monkeypatch, delayed):
    async def scenario():
        buses = []
        def new_bus():
            bus = FakeBus(delayed=delayed)
            buses.append(bus)
            return bus

        monkeypatch.setattr(dbus_next.aio, "MessageBus", new_bus)
        inhibitor = IdleInhibitor()
        inhibitor._enabled = True
        inhibitor.set_active(True)
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        first = buses[0]
        assert len(buses) == 1
        message = first.calls[0]
        assert message.destination == "org.freedesktop.ScreenSaver"
        assert message.path == "/org/freedesktop/ScreenSaver"
        assert message.member == "Inhibit" and message.signature == "ss"
        inhibitor.set_active(False)
        assert first.disconnected
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        assert inhibitor._bus is buses[1]
        first.reply.set()  # superseded work cannot keep the desktop awake
        await asyncio.sleep(0)
        assert inhibitor._bus is buses[1] and not buses[1].disconnected
        inhibitor.set_active(False)
        await asyncio.sleep(0)
        assert all(bus.disconnected for bus in buses)

    asyncio.run(scenario())


def test_missing_service_is_nonfatal_and_closes_connection(monkeypatch, caplog):
    async def scenario():
        bus = FakeBus(error=True)
        monkeypatch.setattr(dbus_next.aio, "MessageBus", lambda: bus)
        inhibitor = IdleInhibitor()
        inhibitor._enabled = True
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        assert bus.disconnected
        assert inhibitor._bus is None
        assert "Could not inhibit screen idle" in caplog.text
        inhibitor.set_active(False)

    asyncio.run(scenario())


def test_headless_inhibitor_never_connects(monkeypatch):
    def unexpected_connection():
        pytest.fail("headless playback must not inhibit the user's desktop")

    monkeypatch.setattr(dbus_next.aio, "MessageBus", unexpected_connection)
    inhibitor = IdleInhibitor(enabled=False)
    inhibitor.set_active(True)  # works even without an event loop
    assert inhibitor._task is None
