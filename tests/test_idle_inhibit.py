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
        self.lost = None
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

    def lose(self, error):
        """The bus daemon drops the connection."""
        self.lost = error
        self.closed.set()

    async def wait_for_disconnect(self):
        await self.closed.wait()
        if self.lost is not None:
            raise self.lost


class Buses(list):
    """Buses created so far. Kinds ("session", "system") in `missing` cannot
    open their socket; those in `failing` answer every call with an error."""

    def __init__(self):
        super().__init__()
        self.missing = set()
        self.failing = set()


@pytest.fixture
def buses(monkeypatch):
    created = Buses()

    def new_bus(bus_type=BusType.SESSION, negotiate_unix_fd=False):
        kind = "system" if bus_type == BusType.SYSTEM else "session"
        if kind in created.missing:
            # dbus_next's constructor connects the socket.
            raise FileNotFoundError(f"no {kind} bus socket")
        bus = FakeBus(bus_type, negotiate_unix_fd, error=kind in created.failing)
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
    # Until the read end sees end-of-file the write end is still open, so its
    # number cannot have been reused yet.
    if not lock_released(read_end):
        os.close(write_end)
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
    buses.failing = {"session", "system"}

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


def test_missing_bus_leaves_the_other_inhibitor_working(buses, lock_pipe, caplog):
    read_end, write_end = lock_pipe
    buses.missing = {"session"}

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        tasks = list(inhibitor._tasks)
        await asyncio.sleep(0)
        (system,) = buses
        system.deliver(write_end)
        await asyncio.sleep(0)
        assert all(task.done() and task.exception() is None for task in tasks)
        assert inhibitor._tasks == [] and inhibitor._lock_fds == [write_end]
        assert "through org.freedesktop.ScreenSaver failed: no session bus socket" in caplog.text
        inhibitor.set_active(False)
        assert lock_released(read_end)

    asyncio.run(scenario())


def test_missing_system_bus_leaves_the_screensaver_inhibitor_held(buses, caplog):
    buses.missing = {"system"}

    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        logind = inhibitor._tasks[1]
        await asyncio.sleep(0)
        (session,) = buses
        session.deliver()
        await asyncio.sleep(0)
        assert logind.done() and logind.exception() is None
        assert "through org.freedesktop.login1 failed: no system bus socket" in caplog.text
        assert len(inhibitor._tasks) == 1 and not session.disconnected
        inhibitor.set_active(False)
        assert session.disconnected

    asyncio.run(scenario())


def test_lost_session_bus_is_a_warning(buses, caplog):
    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        screensaver = inhibitor._tasks[0]
        await asyncio.sleep(0)
        (session,), (system,) = by_kind(buses)
        session.deliver()
        system.deliver()
        await asyncio.sleep(0)
        session.lose(EOFError("bus daemon went away"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert screensaver.done() and screensaver.exception() is None
        assert "through org.freedesktop.ScreenSaver failed: bus daemon went away" in caplog.text
        assert inhibitor._tasks == [] and inhibitor._buses == []
        inhibitor.set_active(False)

    asyncio.run(scenario())


def test_release_before_requests_start_never_connects(buses):
    async def scenario():
        inhibitor = enabled_inhibitor()
        inhibitor.set_active(True)
        inhibitor.set_active(False)
        await asyncio.sleep(0)
        assert buses == [] and inhibitor._tasks == [] and inhibitor._buses == []

    asyncio.run(scenario())


def test_headless_inhibitor_never_connects(monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("headless playback must not inhibit the user's desktop")

    monkeypatch.setattr(dbus_next.aio, "MessageBus", unexpected_connection)
    inhibitor = IdleInhibitor(enabled=False)
    inhibitor.set_active(True)  # works even without an event loop
    assert inhibitor._tasks == []
