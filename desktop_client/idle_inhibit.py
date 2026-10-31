"""Linux desktop idle inhibition, independent of an mpv-owned video window."""

from __future__ import annotations

import asyncio
import logging
import os
import sys


log = logging.getLogger(__name__)

_WHO = "upscale-relay-client"
_WHY = "Playing video"


class IdleInhibitor:
    """Keep the desktop awake while video plays.

    Idle handlers each honour their own inhibitors, so this holds two: the
    org.freedesktop.ScreenSaver session service's (Hypridle, KDE…) and a
    logind idle block lock. Handlers that cannot see the ScreenSaver
    service's list read logind's BlockInhibited instead: with only the
    ScreenSaver inhibitor held, a Quickshell dimmer blanked playback after
    its timeout while Hypridle stayed inhibited. A Wayland idle inhibitor
    would cover both, but it belongs to the window's wl_surface, which
    neither Qt nor PySide6 exposes.
    """

    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled and sys.platform.startswith("linux")
        self._active = False
        self._tasks: list[asyncio.Task] = []
        self._buses: list = []
        self._lock_fds: list[int] = []

    def set_active(self, active: bool) -> None:
        if not self._enabled or active == self._active:
            return
        self._active = active
        if active:
            self._tasks = [
                asyncio.create_task(self._run(
                    self._hold_screensaver, "org.freedesktop.ScreenSaver")),
                asyncio.create_task(self._run(
                    self._take_logind_lock, "org.freedesktop.login1")),
            ]
            return
        # Both services drop a caller's inhibitor when it goes away: the
        # ScreenSaver service when its connection closes, logind when the
        # lock's file descriptor does. Closing those also revokes a request
        # whose reply has not arrived yet, without a nested event loop or
        # waiting for a reply during application exit.
        for bus in self._buses:
            bus.disconnect()
        self._buses.clear()
        for fd in self._lock_fds:
            os.close(fd)
        self._lock_fds.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()

    async def _run(self, inhibit, service: str) -> None:
        try:
            await inhibit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # An unavailable desktop service must not interrupt playback.
            log.warning("Idle inhibition through %s failed: %s", service, error)
        finally:
            task = asyncio.current_task()
            if task in self._tasks:
                self._tasks.remove(task)

    async def _hold_screensaver(self) -> None:
        from dbus_next import Message
        from dbus_next.aio import MessageBus

        # The constructor already connects: a missing bus raises here.
        bus = MessageBus()
        try:
            await self._request(bus, Message(
                destination="org.freedesktop.ScreenSaver",
                path="/org/freedesktop/ScreenSaver",
                interface="org.freedesktop.ScreenSaver",
                member="Inhibit",
                signature="ss",
                body=[_WHO, _WHY],
            ))
            # Raises if the bus drops the connection; returns after our own
            # disconnect.
            await bus.wait_for_disconnect()
        finally:
            self._drop(bus)

    async def _take_logind_lock(self) -> None:
        from dbus_next import BusType, Message
        from dbus_next.aio import MessageBus

        bus = MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)
        message = Message(
            destination="org.freedesktop.login1",
            path="/org/freedesktop/login1",
            interface="org.freedesktop.login1.Manager",
            member="Inhibit",
            signature="ssss",
            body=["idle", _WHO, _WHY, "block"],
        )

        def adopt_lock(reply) -> None:
            # Runs as the reply is read. A release before this task resumes
            # would otherwise lose the descriptor with the reply, and the
            # lock would last until the application exits.
            if reply.reply_serial != message.serial:
                return
            if bus in self._buses:
                self._lock_fds.extend(reply.unix_fds)
            else:
                for fd in reply.unix_fds:
                    os.close(fd)

        bus.add_message_handler(adopt_lock)
        try:
            await self._request(bus, message)
        finally:
            # The descriptor alone holds the lock.
            self._drop(bus)

    async def _request(self, bus, message) -> None:
        from dbus_next import MessageType

        self._buses.append(bus)
        async with asyncio.timeout(5):
            await bus.connect()
            reply = await bus.call(message)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")

    def _drop(self, bus) -> None:
        bus.disconnect()
        if bus in self._buses:
            self._buses.remove(bus)
