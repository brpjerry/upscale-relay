"""The desktop asks /status before reusing a server whose teardown went unacknowledged.

A control connection that is already dead at teardown (suspend/resume, Wi-Fi
drop) used to end at "server teardown unconfirmed" until the user connected
again. The window now remembers that session, reconnects, and lets no session
open until the server reports it released.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("qasync")

import desktop_client.main_window as main_window
from test_server_library_gui import window, FakeSessionClient  # noqa: F401

HOME = ("media-server", 8590)
WAITING = "Waiting for the server to release the previous session…"
UNCONFIRMED = ("server did not confirm native session resource release; "
               "do not open a replacement session until the server is checked")


class World:
    """What the fake clients share: the servers and what happened on them."""

    def __init__(self):
        self.events = []
        self.clients = []
        self.held = set()  # (host, port, session_id) the server has not released
        self.control_dead = False  # teardown gets no acknowledgement
        self.names_session = True
        self.unreachable = False  # connect() fails
        self.silent = False  # connect() never answers
        self.status_error = None  # what asking /status raises
        self.opened = 0
        self.violations = []

    def count(self, kind):
        return sum(1 for event in self.events if event[0] == kind)

    def release(self):
        self.held.clear()


class WorldClient(FakeSessionClient):
    def __init__(self, world, host=HOME[0], port=HOME[1]):
        super().__init__()
        self.world = world
        self.host, self.port = host, port
        self.connected = True
        self.closed = False
        self.epoch = 0
        world.clients.append(self)

    async def connect(self):
        if self.world.unreachable:
            raise ConnectionError("unreachable")
        if self.world.silent:
            await asyncio.Event().wait()
        self.world.events.append(("connect", self.host, self.port))
        return {"server_name": "test", "models": [{"name": "passthrough"}]}

    async def open_session(self, config):
        if any(held[:2] == (self.host, self.port) for held in self.world.held):
            self.world.violations.append(config.path)
        self.world.opened += 1
        self.world.events.append(("open", config.path, self.host))
        session = await super().open_session(config)
        session.session_id = f"session-{self.world.opened}"
        session.duration_s = 1000
        return session

    async def seek(self, pts):
        self.epoch += 1

    async def pause(self):
        pass

    async def teardown(self):
        session, self.session = self.session, None
        self.closed, self.connected = True, False
        if session is not None and self.world.control_dead:
            if not self.world.names_session:
                raise main_window.TeardownNotConfirmedError(UNCONFIRMED)
            self.world.held.add((self.host, self.port, session.session_id))
            raise main_window.TeardownNotConfirmedError(
                UNCONFIRMED, session_id=session.session_id)
        self.world.events.append(("closed",))

    async def close(self):
        self.closed = True
        self.connected = False

    async def wait_session_released(self, session_id, **kwargs):
        assert not kwargs  # the core's bound and interval, not the window's
        self.world.events.append(("wait", session_id, self.host))
        while True:
            if self.world.status_error is not None:
                raise self.world.status_error
            if (self.host, self.port, session_id) not in self.world.held:
                self.world.events.append(("released", session_id, self.host))
                return
            await asyncio.sleep(0.005)


@pytest.fixture()
def world(window, monkeypatch):
    world = World()
    world.errors = []
    monkeypatch.setattr(window, "_error", lambda *args: world.errors.append(args))
    monkeypatch.setattr(main_window, "RelayClient",
                        lambda host, port: WorldClient(world, host, port))
    window.client = WorldClient(world)
    return world


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def settled(window):
    await until(lambda: not window._transitioning and window.transitions._task.done())


def run(scenario):
    asyncio.run(scenario())


def test_dead_control_at_stop_recovers_once_the_server_releases(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        first = window.client
        world.control_dead = True
        # What the player reports when the session's downlink ends under it.
        window.player.failed.emit("downlink closed")
        await until(lambda: world.count("wait"))
        assert world.events[-1] == ("wait", "session-1", HOME[0])
        assert window.statusBar().currentMessage() == WAITING
        assert window._transitioning and window.client is not first
        await asyncio.sleep(0.05)
        assert window._transitioning and not world.count("released")

        world.control_dead = False
        world.release()
        await settled(window)
        assert world.errors == [("Playback failed", "downlink closed")]
        assert isinstance(window.client, WorldClient) and not window.client.closed
        assert first.closed
        assert window.conn_label.text() == "connected: test"
        assert window.connect_btn.text() == "Disconnect"
        assert window.statusBar().currentMessage() != WAITING
        assert window._unreleased_session is None

        # Usable: the next session opens without another question.
        await window._start_session("next.mkv", "server_file")
        assert world.events[-1] == ("open", "next.mkv", HOME[0])
        assert world.count("wait") == 1 and not world.violations
        assert world.errors == [("Playback failed", "downlink closed")]

    run(scenario)


def test_session_never_released_stays_the_hard_stop(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        reason = "the server still lists session session-1 60 s after its teardown went unacknowledged"
        world.status_error = main_window.TeardownNotConfirmedError(reason, session_id="session-1")
        await window.on_stop()
        replacement = world.clients[-1]
        assert world.count("wait") == 1 and replacement.closed
        assert window.client is None
        assert window.conn_label.text() == "server teardown unconfirmed"
        assert window.connect_btn.text() == "Connect"
        assert world.errors == [("Server cleanup not confirmed", reason)]
        assert world.opened == 1

        # A manual Connect clears the stop, not the question.
        world.control_dead, world.status_error = False, None
        window.host_edit.setText("media-server:8590")
        await window.on_connect()
        assert window.conn_label.text() == "connected: test"
        assert window._unreleased_session == ("session-1", *HOME)
        opening = asyncio.ensure_future(window._start_session("next.mkv", "server_file"))
        await until(lambda: world.count("wait") == 2)
        await asyncio.sleep(0.05)
        assert world.opened == 1 and not opening.done()
        assert window.statusBar().currentMessage() == WAITING
        world.release()
        await opening
        assert world.events[-1] == ("open", "next.mkv", HOME[0])
        assert window._unreleased_session is None and not world.violations
        assert len(world.errors) == 1

    run(scenario)


def test_no_session_opens_while_the_release_is_pending(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        stopping = asyncio.ensure_future(window.on_stop())
        await until(lambda: world.count("wait") == 1)
        waiting_client = window.client

        # A double-click during the wait: queued behind the question, which is
        # asked again on its behalf.
        world.control_dead = False
        opening = asyncio.ensure_future(window._start_session("other.mkv", "server_file"))
        await until(lambda: world.count("wait") == 2)
        await stopping
        await asyncio.sleep(0.05)
        assert world.opened == 1 and not opening.done()
        assert window.client is waiting_client and not waiting_client.closed
        assert window._unreleased_session == ("session-1", *HOME)
        assert window.statusBar().currentMessage() == WAITING

        world.release()
        await opening
        assert world.events[-2:] == [("released", "session-1", HOME[0]),
                                     ("open", "other.mkv", HOME[0])]
        assert not world.violations and not world.errors
        assert window._session_path == "other.mkv"

    run(scenario)


@pytest.mark.parametrize("next_step", ["another file", "setting restart", "autoplay"])
def test_whatever_followed_the_teardown_proceeds_after_the_release(window, world, monkeypatch, next_step):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        if next_step == "another file":
            following = asyncio.ensure_future(window._start_session("other.mkv", "server_file"))
            expected = "other.mkv"
        elif next_step == "setting restart":
            window._position_s = 234.5
            following = asyncio.ensure_future(window._restart_for_playback_setting())
            expected = "show.mkv"
        else:
            window.settings.autoplay = True

            async def next_sibling(snapshot, valid):
                return "episode-2.mkv"

            monkeypatch.setattr(window, "_next_sibling", next_sibling)
            window._end_session("end of stream")
            following = None
            expected = "episode-2.mkv"
        await until(lambda: world.count("wait") == 1)
        world.control_dead = False
        await asyncio.sleep(0.05)
        assert world.opened == 1
        assert window.statusBar().currentMessage() == WAITING

        world.release()
        if following is not None:
            await following
        await until(lambda: world.opened == 2)
        await settled(window)
        assert world.events[-1] == ("open", expected, HOME[0])
        assert window._session_path == expected
        assert not world.violations and not world.errors
        assert window._unreleased_session is None

    run(scenario)


def test_end_of_stream_without_autoplay_recovers_like_stop(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        window.settings.autoplay = False
        window._end_session("end of stream")
        await until(lambda: world.count("wait") == 1)
        world.release()
        await settled(window)
        assert not world.errors and window._unreleased_session is None
        assert window.conn_label.text() == "connected: test"
        assert window.statusBar().currentMessage() == "end of stream"

    run(scenario)


def test_unanswered_status_is_a_lost_connection_and_the_question_stays(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        world.status_error = ConnectionError("could not ask the server whether session session-1 was released")
        await window.on_stop()
        assert world.clients[-1].closed and window.client is None
        assert window.conn_label.text() == "disconnected"
        assert [title for title, _message in world.errors] == ["Connection failed"]
        assert "session-1" in world.errors[0][1]
        assert window._unreleased_session == ("session-1", *HOME)

        # Not a verdict: the next connection asks again and may be answered.
        world.control_dead, world.status_error = False, None
        window.host_edit.setText("media-server:8590")
        await window.on_connect()
        world.release()
        await window._start_session("next.mkv", "server_file")
        assert world.count("wait") == 2
        assert world.events[-1] == ("open", "next.mkv", HOME[0])
        assert window._unreleased_session is None and not world.violations

    run(scenario)


def test_failed_reconnect_after_an_unconfirmed_teardown_keeps_the_question(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = world.unreachable = True
        await window.on_stop()
        assert window.client is None
        assert window.conn_label.text() == "disconnected"
        assert not world.errors and not world.count("wait")
        assert "Lost the connection" in window.statusBar().currentMessage()
        assert window._unreleased_session == ("session-1", *HOME)

    run(scenario)


@pytest.mark.parametrize("waiting_for", ["stop", "open"])
def test_control_connection_lost_during_the_wait_is_replaced(window, world, monkeypatch, waiting_for):
    monkeypatch.setattr(main_window, "_RECONNECT_DELAYS_S", (0.0,))

    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        if waiting_for == "stop":
            following = asyncio.ensure_future(window.on_stop())
        else:
            following = asyncio.ensure_future(window._start_session("other.mkv", "server_file"))
        await until(lambda: world.count("wait") == 1)
        world.control_dead = False
        # The wait asks over HTTP; the replacement's control socket can die
        # meanwhile, and a transition ignores that.
        asked = window.client
        asked.connected = False
        window._on_control_lost(asked)
        world.release()
        await following
        assert window.client is not asked and window.client.connected and asked.closed
        assert window.conn_label.text() == "connected: test"
        assert not world.errors and not world.violations
        assert window._unreleased_session is None
        if waiting_for == "open":
            assert world.events[-1] == ("open", "other.mkv", HOME[0])
            assert window.client.session is not None

    run(scenario)


def test_reconnect_that_never_answers_is_bounded(window, world, monkeypatch):
    # It runs inside the teardown nothing may interrupt, on a network that
    # just lost the control connection.
    monkeypatch.setattr(main_window, "_UNCONFIRMED_RECONNECT_TIMEOUT_S", 0.05)

    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = world.silent = True
        async with asyncio.timeout(2):
            await window.on_stop()
        assert world.clients[-1].closed and window.client is None
        assert window.conn_label.text() == "disconnected"
        assert not world.errors and not world.count("wait")
        assert window._unreleased_session == ("session-1", *HOME)

    run(scenario)


def test_another_server_does_not_answer_for_the_first(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = world.unreachable = True
        await window.on_stop()
        world.control_dead = world.unreachable = False

        # Asked there too (one machine can have two names), but its answer
        # settles nothing about the server that held the session.
        window.host_edit.setText("other-server:8590")
        await window.on_connect()
        await window._start_session("there.mkv", "server_file")
        assert world.events[-3:] == [("wait", "session-1", "other-server"),
                                     ("released", "session-1", "other-server"),
                                     ("open", "there.mkv", "other-server")]
        assert window._unreleased_session == ("session-1", *HOME)
        await window.on_stop()
        await window.on_connect()  # disconnect

        window.host_edit.setText("media-server:8590")
        await window.on_connect()
        opening = asyncio.ensure_future(window._start_session("here.mkv", "server_file"))
        await until(lambda: world.events[-1] == ("wait", "session-1", HOME[0]))
        await asyncio.sleep(0.05)
        assert not opening.done()
        world.release()
        await opening
        assert world.events[-1] == ("open", "here.mkv", HOME[0])
        assert window._unreleased_session is None
        assert not world.violations and not world.errors

    run(scenario)


def test_teardown_that_names_no_session_is_the_hard_stop_as_before(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead, world.names_session = True, False
        await window.on_stop()
        assert window.client is None and len(world.clients) == 1
        assert window.conn_label.text() == "server teardown unconfirmed"
        assert world.errors == [("Server cleanup not confirmed", UNCONFIRMED)]
        assert not world.count("wait") and window._unreleased_session is None

    run(scenario)


def test_closing_during_the_wait_does_not_wait_it_out(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        stopping = asyncio.ensure_future(window.on_stop())
        await until(lambda: world.count("wait") == 1)
        # What closeEvent runs.
        window._closing = True
        async with asyncio.timeout(2):
            await window._run_transition(window._teardown_session)
            await stopping
        assert world.count("wait") == 1 and not world.count("released")
        assert world.opened == 1 and not world.errors
        assert window._unreleased_session == ("session-1", *HOME)
        window._closing = False

    run(scenario)


def test_disconnecting_with_a_dead_control_keeps_the_question(window, world):
    async def scenario():
        await window._start_session("show.mkv", "server_file")
        world.control_dead = True
        await window.on_connect()  # the Disconnect button
        assert window.client is None and window.conn_label.text() == "disconnected"
        assert not world.errors and not world.count("wait")
        assert window._unreleased_session == ("session-1", *HOME)

    run(scenario)


def test_local_fallback_proceeds_and_remembers_the_unreleased_session(window, world):
    async def scenario():
        await window._start_session("/videos/show.mkv", "uplink")
        world.control_dead = True
        await window.on_fallback()
        assert window._session_source == "local"
        assert window.player.local_playback[0] == "/videos/show.mkv"
        assert window.client is None and window.conn_label.text() == "disconnected"
        assert [title for title, _message in world.errors] == ["Server cleanup not confirmed"]
        assert "next upscaled session" in world.errors[0][1]
        assert window._unreleased_session == ("session-1", *HOME)

        world.control_dead = False
        window.host_edit.setText("media-server:8590")
        await window.on_connect()
        opening = asyncio.ensure_future(window._start_session("/videos/next.mkv", "uplink"))
        await until(lambda: world.count("wait") == 1)
        await asyncio.sleep(0.05)
        assert world.opened == 1
        world.release()
        await opening
        assert world.opened == 2 and not world.violations

    run(scenario)
