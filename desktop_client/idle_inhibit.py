"""Linux desktop idle inhibition, independent of an mpv-owned video window."""

from __future__ import annotations

import asyncio
import logging
import sys


log = logging.getLogger(__name__)


class IdleInhibitor:
    def __init__(self, *, enabled: bool = True):
        self._enabled = enabled and sys.platform.startswith("linux")
        self._active = False
        self._task: asyncio.Task | None = None
        self._bus = None

    def set_active(self, active: bool) -> None:
        if not self._enabled or active == self._active:
            return
        self._active = active
        if active:
            self._task = asyncio.create_task(self._hold())
        else:
            # The standard ScreenSaver service releases a caller's inhibitors
            # on disconnect. Use a dedicated connection so pause/stop can also
            # revoke an Inhibit whose reply has not arrived yet, without a
            # nested event loop or waiting for a cookie during application exit.
            if self._bus is not None:
                self._bus.disconnect()
                self._bus = None
            if self._task is not None:
                self._task.cancel()
                self._task = None

    async def _hold(self) -> None:
        bus = None
        try:
            from dbus_next import Message, MessageType
            from dbus_next.aio import MessageBus

            bus = self._bus = MessageBus()
            async with asyncio.timeout(5):
                await bus.connect()
                reply = await bus.call(Message(
                    destination="org.freedesktop.ScreenSaver",
                    path="/org/freedesktop/ScreenSaver",
                    interface="org.freedesktop.ScreenSaver",
                    member="Inhibit",
                    signature="ss",
                    body=["upscale-relay-client", "Playing video"],
                ))
                if reply.message_type == MessageType.ERROR:
                    raise RuntimeError(f"{reply.error_name}: {reply.body}")
            await bus.wait_for_disconnect()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # An unavailable desktop service must not interrupt playback.
            log.warning("Could not inhibit screen idle: %s", error)
        finally:
            if bus is not None:
                bus.disconnect()
            if self._bus is bus:
                self._bus = None
            if self._task is asyncio.current_task():
                self._task = None
