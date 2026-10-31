"""Desktop inhibition follows playback without leaking delayed requests."""

import asyncio
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("dbus_next")
from dbus_next import BusType, MessageType
import dbus_next.aio

from desktop_client.idle_inhibit import IdleInhibitor


class FakeBus:
    """Reads a reply (running message handlers) apart from resuming its caller,
    as dbus_next does."""

    def __init__(self, bus_type=BusType.SESSION, negotiate_unix_fd=False, *, error=False):
        self.system = bus_type == BusType.SYSTEM
        self.negotiate_unix_fd = negotiate_unix_fd
        self.error = error
        self.calls = []
        self.handlers = []
        self.disconnected = False
        self.closed = asyncio.Event()
        self.pending = None

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    async def connect(self):
        return self

    async def call(self, message):
        message.serial = 2
        self.calls.append(message)
        self.pending = asyncio.get_running_loop().create_future()
        if self.error:
            self.deliver()
        return await self.pending

    def deliver(self, *unix_fds):
        reply = SimpleNamespace(
            message_type=MessageType.ERROR if self.error else MessageType.METHOD_RETURN,
            error_name="org.freedesktop.DBus.Error.ServiceUnknown",
            body=[0 if self.system else 7], reply_serial=self.calls[-1].serial,
            unix_fds=list(unix_fds),
        )
        for handler in self.handlers:
            handler(reply)
        if not self.pending.done():
            self.pending.set_result(reply)

    def disconnect(self):
        self.disconnected = True
        self.closed.set()
        if self.pending is not None and not self.pending.done():
            self.pending.set_exception(EOFError("connection closed"))

    async def wait_for_disconnect(self):
        await self.closed.wait()


class Buses(list):
    error = False  # every bus created from now on answers with an error


@pytest.fixture
def buses(monkeypatch):
    created = Buses()

    def new_bus(bus_type=BusType.SESSION, negotiate_unix_fd=False):
        bus = FakeBus(bus_type, negotiate_unix_fd, error=created.error)
        created.append(bus)
        return bus

    monkeypatch.setattr(dbus_next.aio, "MessageBus", new_bus)
    return created


@pytest.fixture
def lock_pipe():
    # logind keeps the read end and drops the lock at end-of-file.
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    yield read_end, write_end
    os.close(read_end)


def lock_released(read_end):
    try:
        return os.read(read_end, 1) == b""
    except BlockingIOError:
        return False


def enabled_inhibitor():
    inhibitor = IdleInhibitor()
    inhibitor._enabled = True
    return inhibitor


def by_kind(buses):
    session = [bus for bus in buses if not bus.system]
    system = [bus for bus in buses if bus.system]
    return session, system


def test_both_inhibitors_are_requested(buses):
    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        (session,), (system,) = by_kind(buses)
        message = session.calls[0]
        assert message.destination == "org.freedesktop.ScreenSaver"
        assert message.path == "/org/freedesktop/ScreenSaver"
        assert message.member == "Inhibit" and message.signature == "ss"
        message = system.calls[0]
        assert system.negotiate_unix_fd
        assert message.destination == "org.freedesktop.login1"
        assert message.interface == "org.freedesktop.login1.Manager"
        assert message.member == "Inhibit" and message.signature == "ssss"
        assert message.body[0] == "idle" and message.body[3] == "block"
        inhibitor.set_active(False)

    asyncio.run(scenario())


def test_pause_revokes_unanswered_requests_and_resume_reconnects(buses, lock_pipe):
    read_end, write_end = lock_pipe

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        assert len(buses) == 2
        inhibitor.set_active(False)
        assert all(bus.disconnected for bus in buses)
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        assert len(buses) == 4
        (_, session), (old_system, system) = by_kind(buses)
        # A reply already in the socket is still read after the disconnect.
        # Superseded work cannot keep the desktop awake.
        old_system.deliver(write_end)
        assert lock_released(read_end)
        await asyncio.sleep(0)
        assert inhibitor._lock_fds == []
        assert not session.disconnected and not system.disconnected
        inhibitor.set_active(False)
        await asyncio.sleep(0)
        assert all(bus.disconnected for bus in buses)
        assert inhibitor._buses == [] and inhibitor._tasks == []

    asyncio.run(scenario())


def test_logind_lock_is_its_descriptor_until_release(buses, lock_pipe):
    read_end, write_end = lock_pipe

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        (session,), (system,) = by_kind(buses)
        session.deliver()
        system.deliver(write_end)
        await asyncio.sleep(0)
        # The descriptor alone holds the lock; its connection is closed.
        assert system.disconnected and not session.disconnected
        assert inhibitor._lock_fds == [write_end]
        assert not lock_released(read_end)
        inhibitor.set_active(False)
        assert lock_released(read_end) and session.disconnected
        await asyncio.sleep(0)
        assert inhibitor._buses == [] and inhibitor._tasks == []

    asyncio.run(scenario())


def test_release_between_reply_and_resume_still_drops_the_lock(buses, lock_pipe):
    read_end, write_end = lock_pipe

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        _, (system,) = by_kind(buses)
        system.deliver(write_end)
        inhibitor.set_active(False)  # before the request's task resumes
        assert lock_released(read_end)
        await asyncio.sleep(0)
        assert inhibitor._lock_fds == [] and inhibitor._tasks == []

    asyncio.run(scenario())


def test_missing_services_are_nonfatal_and_close_connections(buses, caplog):
    buses.error = True

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(buses) == 2 and all(bus.disconnected for bus in buses)
        assert inhibitor._buses == [] and inhibitor._tasks == []
        assert "through org.freedesktop.ScreenSaver" in caplog.text
        assert "through org.freedesktop.login1" in caplog.text
        inhibitor.set_active(False)

    asyncio.run(scenario())


def test_headless_inhibitor_never_connects(monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("headless playback must not inhibit the user's desktop")

    monkeypatch.setattr(dbus_next.aio, "MessageBus", unexpected_connection)
    inhibitor = IdleInhibitor(enabled=False)
    inhibitor.set_active(True)  # works even without an event loop
    assert inhibitor._tasks == []
