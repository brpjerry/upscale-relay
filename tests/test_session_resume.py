"""A session outlives its control connection and resumes on a new one.

docs/PROTOCOL.md §1.2: a client that asked for resume gets a token in
session_opened; when the control connection drops, the server holds the
session for a grace period, and a new connection that names the session and
its token takes it over. Media connections are left alone throughout.
"""
import asyncio
import json
import threading

import aiohttp
import pytest

import relay_client_core.client as client_module
from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig
from relay_client_core.client import TeardownNotConfirmedError
from relay_protocol import PROTOCOL_VERSION
from relay_server.pipeline import Pipeline, PipelineCloseError
from relay_server.server import RelayServer
from relay_server.session import Session
from upscale_cli.sample import make_sample


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "a.mkv"
    make_sample(str(path), frames=1200, width=64, height=64, fps=10)
    return str(path)


def _config(source: str, **fields) -> SessionConfig:
    return SessionConfig(path=source, model="passthrough", quality_tier="lossless-ffv1",
                         display_w=64, display_h=64, **fields)


def _run(scenario, limit: float = 40):
    async def bounded():
        async with asyncio.timeout(limit):
            await scenario()
    asyncio.run(bounded())


async def _until(predicate, interval: float = 0.02):
    while not predicate():
        await asyncio.sleep(interval)


async def _status(server: RelayServer) -> dict:
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{server.port}/status") as reply:
            return await reply.json()


async def _playing_client(server: RelayServer, source: str, **fields):
    client = RelayClient("127.0.0.1", server.port)
    await client.connect()
    info = await client.open_session(_config(source, **fields))
    await client.attach_media()
    await client.start_uplink()
    await client.play()
    assert await client.downlink_queue().get() is not None
    return client, info


# Bugs caught: 0, False positives: 0
def test_a_dropped_control_connection_resumes_the_same_session(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            assert client.capabilities["session_resume"] is True
            assert info.resume_token and info.resume_grace_s == 120.0
            events = []
            client.on_reconnecting = lambda: events.append("reconnecting")
            client.on_reconnected = lambda: events.append("reconnected")
            client.on_disconnected = lambda: events.append("disconnected")
            held = server.sessions[info.session_id]

            # No close frame and no teardown: the control socket just goes away.
            client._ws._conn.transport.abort()
            await _until(lambda: "reconnected" in events)
            assert events == ["reconnecting", "reconnected"]
            assert client.connected and client.session is info
            # The same server session, now on the new connection.
            assert server.sessions[info.session_id] is held
            assert [s["control_attached"] for s in (await _status(server))["sessions"]] == [True]
            # Media went on over the connections it already had, and the
            # session still answers requests.
            assert await client.downlink_queue().get() is not None
            await client.pause()
            await _until(lambda: client.state == "paused")
            await client.play()
            await client.teardown()  # acknowledged: raises nothing
            assert info.session_id not in server.sessions
            assert server.native_teardown_error is None
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 1
def test_a_seek_made_while_reconnecting_is_sent_once_resumed(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            reconnected = asyncio.Event()
            client.on_reconnected = reconnected.set
            # Hold the reconnect until the seek is waiting for it.
            gate = asyncio.Event()
            resume_once = client._resume_once

            async def gated(session):
                await gate.wait()
                return await resume_once(session)

            client._resume_once = gated
            client._ws._conn.transport.abort()
            await _until(lambda: client.resuming)
            seek = asyncio.create_task(client.seek(target_pts=0))
            await asyncio.sleep(0.2)
            assert not seek.done()
            gate.set()
            await seek
            assert reconnected.is_set()
            assert server.sessions[info.session_id].epoch == client.epoch == 1
            assert await client.downlink_queue().get() is not None
            await client.teardown()
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_an_unreachable_server_releases_the_held_session_after_its_grace(source, monkeypatch):
    monkeypatch.setattr(client_module, "CONTROL_HEARTBEAT_S", 0.4)
    monkeypatch.setattr(client_module, "RESUME_ATTEMPT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(client_module, "RESUME_RETRY_DELAYS_S", (0.1,))
    monkeypatch.setattr(client_module, "RESUME_RETRY_MAX_S", 0.2)

    async def scenario():
        server = RelayServer("models", free_port_pair(), resume_grace_s=1.5)
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            assert info.resume_grace_s == 1.5
            events = []
            client.on_reconnecting = lambda: events.append("reconnecting")
            client.on_disconnected = lambda: events.append("disconnected")
            # Reconnecting finds nothing listening.
            client.port = free_port_pair()
            client._ws._conn.transport.abort()

            # The server holds the session meanwhile, producing nothing more.
            await _until(lambda: server.sessions[info.session_id].ws is None)
            entry, = (await _status(server))["sessions"]
            assert entry["id"] == info.session_id and entry["control_attached"] is False
            pipeline = server.sessions[info.session_id].pipeline
            await _until(lambda: pipeline.stats.paused_for_backpressure)

            # Then it releases it, as for any session whose client vanished.
            await _until(lambda: info.session_id not in server.sessions)
            status = await _status(server)
            assert status["sessions"] == [] and status["restart_required"] is False
            # The client gives up once the grace period has certainly passed.
            await _until(lambda: "disconnected" in events)
            assert events == ["reconnecting", "disconnected"]
            assert not client.connected and not client.resuming
            # A consumer waiting on the half-open downlink wakes up.
            while await client.downlink_queue().get() is not None:
                pass
            assert client.source_error == "The connection to the server was lost"
            with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                await client.teardown()
            assert unconfirmed.value.session_id == info.session_id
            assert unconfirmed.value.resume_token == info.resume_token
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_the_next_connection_releases_a_held_session_by_resuming_it(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        lost = replacement = None
        try:
            lost, info = await _playing_client(server, source)
            lost._ws._conn.transport.abort()
            # Stopped before it could resume: the teardown never reached the
            # server, which holds the session for two minutes.
            with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                await lost.teardown()
            assert unconfirmed.value.session_id == info.session_id
            assert unconfirmed.value.resume_token == info.resume_token
            await _until(lambda: server.sessions[info.session_id].ws is None)

            replacement = RelayClient("127.0.0.1", server.port)
            await replacement.connect()
            lost_events = []
            replacement.on_disconnected = lambda: lost_events.append(True)
            async with asyncio.timeout(5):  # not the 60 s /status bound
                await replacement.wait_session_released(
                    info.session_id, resume_token=unconfirmed.value.resume_token)
            assert info.session_id not in server.sessions
            assert server.native_teardown_error is None
            # The server ended that connection after the teardown, as it does
            # after any; that is no lost connection.
            assert not replacement.connected and lost_events == []

            # Without the token, or once it is gone, /status answers.
            again = RelayClient("127.0.0.1", server.port)
            try:
                await again.connect()
                await again.wait_session_released(
                    info.session_id, resume_token=unconfirmed.value.resume_token,
                    timeout=1.0)
            finally:
                await again.close()
        finally:
            for client in (lost, replacement):
                if client is not None:
                    await client.close()
            await server.stop()

    _run(scenario)


async def _control(port: int):
    http = aiohttp.ClientSession()
    ws = await http.ws_connect(f"http://127.0.0.1:{port}/control")
    await ws.send_str(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION}))
    assert json.loads((await ws.receive()).data)["type"] == "capabilities"
    return http, ws


async def _receive(ws, kind: str) -> dict:
    while True:
        msg = json.loads((await ws.receive()).data)
        if msg["type"] == kind:
            return msg


# Bugs caught: 0, False positives: 0
def test_a_resume_takes_the_session_from_a_connection_the_server_thinks_alive(source, tmp_path):
    # The client gives up on a connection first (its own heartbeat), and its
    # resume arrives while the server still holds the old one.
    async def scenario():
        server = RelayServer("models", free_port_pair(), library_root=str(tmp_path))
        await server.start()
        sockets = []
        try:
            http, old = await _control(server.port)
            sockets.append(http)
            await old.send_str(json.dumps({
                "type": "open_session", "source": {"type": "server_file", "path": "a.mkv"},
                "model": "passthrough", "quality_tier": "lossless-ffv1",
                "display": {"w": 64, "h": 64}, "resume": True,
            }))
            opened = await _receive(old, "session_opened")
            session = server.sessions[opened["session_id"]]
            assert session.ws is not None

            http, new = await _control(server.port)
            sockets.append(http)
            await new.send_str(json.dumps({
                "type": "resume_session", "session_id": opened["session_id"],
                "resume_token": "0" * len(opened["resume_token"]),
            }))
            refused = await _receive(new, "error")
            assert refused["code"] == "resume_failed" and refused["fatal"] is False

            await new.send_str(json.dumps({
                "type": "resume_session", "session_id": opened["session_id"],
                "resume_token": opened["resume_token"],
            }))
            resumed = await _receive(new, "session_resumed")
            assert resumed["session_id"] == opened["session_id"]
            assert resumed["state"] == "open" and resumed["epoch"] == 0
            # The old connection is closed, and its end does not end the session.
            assert (await old.receive()).type in (
                aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
            await asyncio.sleep(0.2)
            assert server.sessions[opened["session_id"]] is session
            assert session.ws is not None and not session.closing.is_set()

            await new.send_str(json.dumps({"type": "teardown"}))
            await _receive(new, "closed")
            assert opened["session_id"] not in server.sessions
        finally:
            for http in sockets:
                await http.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
@pytest.mark.parametrize("grace, asked", [(0.0, True), (120.0, False)])
def test_sessions_that_did_not_ask_for_resume_end_with_their_connection(source, grace, asked):
    # Older clients (Android) rely on the 45 s release bound of PROTOCOL §1.1.
    async def scenario():
        server = RelayServer("models", free_port_pair(), resume_grace_s=grace)
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source, resume=asked)
            assert client.capabilities["session_resume"] is (grace > 0)
            assert info.resume_token is None and info.resume_grace_s is None
            lost = []
            client.on_disconnected = lambda: lost.append(True)
            client._ws._conn.transport.abort()
            await _until(lambda: lost)
            assert not client.resuming
            await _until(lambda: info.session_id not in server.sessions)
        finally:
            if client is not None:
                with pytest.raises(TeardownNotConfirmedError):
                    await client.teardown()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_normal_close_ends_the_session_at_once(source):
    # A client that closes its connection itself is there, and chose to end.
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            client._closing = True  # no resume from this end either
            await client._ws.close()
            await _until(lambda: info.session_id not in server.sessions)
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_the_client_tries_for_as_long_as_the_server_may_hold_the_session():
    # The server holds a session for its grace period from when it notices the
    # loss, up to a heartbeat window after the client did; the client gives up
    # only after grace + its own heartbeat window, so it never gives up on a
    # session the server still holds (docs/PROTOCOL.md §1.2, step 5).
    from relay_server import server as server_module
    assert client_module.CONTROL_HEARTBEAT_S >= server_module.CONTROL_HEARTBEAT_S
    assert server_module.SESSION_RESUME_GRACE_S == 120.0  # as PROTOCOL.md §1.1 says


# Bugs caught: 0, False positives: 0
def test_an_attempt_whose_reply_is_late_does_not_end_the_session(source, monkeypatch):
    # The server took the session over and its reply is late: the client gives
    # the attempt up, which must leave the session held for the next one.
    monkeypatch.setattr(client_module, "RESUME_ATTEMPT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(client_module, "RESUME_RETRY_DELAYS_S", (0.1,))
    send = Session.send
    delayed = []

    async def late_first_reply(self, type_, **fields):
        if type_ == "session_resumed" and not delayed:
            delayed.append(True)
            await asyncio.sleep(1.0)
        return await send(self, type_, **fields)

    monkeypatch.setattr(Session, "send", late_first_reply)

    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            held = server.sessions[info.session_id]
            reconnected = asyncio.Event()
            client.on_reconnected = reconnected.set
            client._ws._conn.transport.abort()
            await reconnected.wait()
            assert delayed and server.sessions[info.session_id] is held
            assert held.ws is not None and not held.closing.is_set()
            await client.teardown()
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_media_connection_lost_with_the_control_one_ends_playback(source):
    # Not re-attached (PROTOCOL §1.2): the consumer must not wait forever.
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            client._uplink_writer.transport.abort()
            await _until(lambda: not server.sessions[info.session_id].uplink_attached)
            reconnected = asyncio.Event()
            client.on_reconnected = reconnected.set
            client._ws._conn.transport.abort()
            await reconnected.wait()
            while await client.downlink_queue().get() is not None:
                pass
            assert client.source_error == "A media connection to the server was lost"
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_seek_written_as_the_connection_dies_is_sent_once_resumed(source):
    # The write fails before the reader has seen the connection end.
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            client._ws._conn.transport.abort()
            await client.seek(target_pts=0)
            assert server.sessions[info.session_id].epoch == client.epoch == 1
            await client.teardown()
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_takeover_reports_a_seek_only_once_it_is_flushed(source, tmp_path, monkeypatch):
    flushing, release = threading.Event(), threading.Event()
    flush = Pipeline.flush

    def slow_flush(self, *args, **kwargs):
        flushing.set()
        assert release.wait(10)
        return flush(self, *args, **kwargs)

    monkeypatch.setattr(Pipeline, "flush", slow_flush)

    async def scenario():
        server = RelayServer("models", free_port_pair(), library_root=str(tmp_path))
        await server.start()
        sockets = []
        try:
            http, old = await _control(server.port)
            sockets.append(http)
            await old.send_str(json.dumps({
                "type": "open_session", "source": {"type": "server_file", "path": "a.mkv"},
                "model": "passthrough", "quality_tier": "lossless-ffv1",
                "display": {"w": 64, "h": 64}, "resume": True,
            }))
            opened = await _receive(old, "session_opened")
            await old.send_str(json.dumps({"type": "seek", "target_pts": 0, "epoch": 1}))
            assert await asyncio.to_thread(flushing.wait, 10)

            http, new = await _control(server.port)
            sockets.append(http)
            await new.send_str(json.dumps({
                "type": "resume_session", "session_id": opened["session_id"],
                "resume_token": opened["resume_token"],
            }))
            reply = asyncio.ensure_future(_receive(new, "session_resumed"))
            await asyncio.sleep(0.3)
            assert not reply.done()  # the seek is still flushing
            release.set()
            assert (await reply)["epoch"] == 1
        finally:
            release.set()
            for http in sockets:
                await http.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 1
def test_shutdown_reports_a_held_session_that_failed_to_close(source, monkeypatch):
    real_close = Pipeline.close

    def failing_close(self, *args, **kwargs):
        real_close(self, *args, **kwargs)  # its workers stop; it reports failure
        raise PipelineCloseError("fake-finish survived")

    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            client.port = free_port_pair()  # its resume goes nowhere
            client._ws._conn.transport.abort()
            await _until(lambda: server.sessions[info.session_id].ws is None)
            monkeypatch.setattr(Pipeline, "close", failing_close)
            with pytest.raises(RuntimeError, match="held session"):
                await server.stop()
            assert server.native_teardown_error["session_id"] == info.session_id
        finally:
            if client is not None:
                await client.close()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_release_by_resume_still_reports_a_server_that_needs_a_restart(source):
    # `closed` settles this session; /status still says whether the server
    # must restart because another session's close failed.
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        lost = replacement = None
        try:
            lost, info = await _playing_client(server, source)
            lost._ws._conn.transport.abort()
            with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                await lost.teardown()
            await _until(lambda: server.sessions[info.session_id].ws is None)
            server.native_teardown_error = {
                "session_id": "other", "error": "boom", "restart_required": True}
            replacement = RelayClient("127.0.0.1", server.port)
            await replacement.connect()
            with pytest.raises(TeardownNotConfirmedError, match="restart"):
                await replacement.wait_session_released(
                    info.session_id, resume_token=unconfirmed.value.resume_token)
            assert info.session_id not in server.sessions  # released all the same
        finally:
            for client in (lost, replacement):
                if client is not None:
                    await client.close()
            with pytest.raises(RuntimeError, match="native session teardown"):
                await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_client_reconnected_after_a_release_resumes_its_next_session(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        lost = client = None
        try:
            lost, info = await _playing_client(server, source)
            lost._ws._conn.transport.abort()
            with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                await lost.teardown()
            client = RelayClient("127.0.0.1", server.port)
            await client.connect()
            await client.wait_session_released(
                info.session_id, resume_token=unconfirmed.value.resume_token)
            # The same client, connected again, plays and loses its connection.
            await client.connect()
            await client.open_session(_config(source))
            await client.attach_media()
            await client.start_uplink()
            await client.play()
            reconnected = asyncio.Event()
            client.on_reconnected = reconnected.set
            client._ws._conn.transport.abort()
            await reconnected.wait()
            await client.teardown()
        finally:
            for each in (lost, client):
                if each is not None:
                    await each.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_held_session_that_fails_is_released_at_once(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        try:
            client, info = await _playing_client(server, source)
            client.port = free_port_pair()  # its resume goes nowhere
            client._ws._conn.transport.abort()
            held = server.sessions[info.session_id]
            await _until(lambda: held.ws is None)
            held._pipeline_error("the encoder failed")
            async with asyncio.timeout(5):  # not the 120 s grace period
                await _until(lambda: info.session_id not in server.sessions)
            assert server.native_teardown_error is None
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_client_whose_session_another_connection_resumed_does_not_take_it_back(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        http = aiohttp.ClientSession()
        try:
            client, info = await _playing_client(server, source)
            events = []
            client.on_reconnecting = lambda: events.append("reconnecting")
            client.on_disconnected = lambda: events.append("disconnected")
            other = await http.ws_connect(f"http://127.0.0.1:{server.port}/control")
            await other.send_str(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION}))
            await _receive(other, "capabilities")
            await other.send_str(json.dumps({
                "type": "resume_session", "session_id": info.session_id,
                "resume_token": info.resume_token,
            }))
            await _receive(other, "session_resumed")
            await _until(lambda: events)
            assert events == ["disconnected"] and not client.resuming
            assert server.sessions[info.session_id].ws is not None
            await other.send_str(json.dumps({"type": "teardown"}))
            await _receive(other, "closed")
        finally:
            await http.close()
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 1
def test_a_server_that_no_longer_resumes_gets_no_token(source):
    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        requests = []
        take_over = server._take_over_session

        def recorded(msg, ws):
            requests.append(msg)  # what reached the server, token and all
            return take_over(msg, ws)

        server._take_over_session = recorded
        try:
            client, info = await _playing_client(server, source)
            server.resume_grace_s = 0.0  # as if rolled back: no session_resume
            events = []
            client.on_disconnected = lambda: events.append("disconnected")
            client._ws._conn.transport.abort()
            async with asyncio.timeout(5):
                await _until(lambda: events)
            assert client.capabilities["session_resume"] is False
            assert requests == []  # refused before the token went out
        finally:
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_client_displaced_after_a_resume_gives_the_session_up(source, monkeypatch):
    # The session changes hands while this client resumes it, and once more
    # after: displaced (4001), it must not try again, its consumer wakes, and
    # its teardown does not hand the token on (that would end the new
    # owner's playback).
    monkeypatch.setattr(client_module, "RESUME_RETRY_DELAYS_S", (0.05,))
    monkeypatch.setattr(client_module, "RESUME_RETRY_MAX_S", 0.05)

    async def scenario():
        server = RelayServer("models", free_port_pair())
        await server.start()
        client = None
        http = aiohttp.ClientSession()
        requests = []
        take_over = server._take_over_session

        def recorded(msg, ws):
            requests.append(msg["session_id"])
            return take_over(msg, ws)

        server._take_over_session = recorded
        try:
            client, info = await _playing_client(server, source)
            # This client's resume will hang on its hello until released.
            gate = asyncio.Event()
            resume_once = client._resume_once

            async def gated(session):
                await gate.wait()
                return await resume_once(session)

            client._resume_once = gated
            client._ws._conn.transport.abort()
            await _until(lambda: client.resuming)
            other = await http.ws_connect(f"http://127.0.0.1:{server.port}/control")
            await other.send_str(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION}))
            await _receive(other, "capabilities")
            await other.send_str(json.dumps({
                "type": "resume_session", "session_id": info.session_id,
                "resume_token": info.resume_token,
            }))
            await _receive(other, "session_resumed")
            # This client's own attempt then takes it back from `other`...
            gate.set()
            await _until(lambda: len(requests) >= 2)
            await other.close()
            # ...and a third connection takes it from this client: 4001.
            http2 = aiohttp.ClientSession()
            try:
                again = await http2.ws_connect(f"http://127.0.0.1:{server.port}/control")
                await again.send_str(json.dumps({"type": "hello", "protocol_version": PROTOCOL_VERSION}))
                await _receive(again, "capabilities")
                await again.send_str(json.dumps({
                    "type": "resume_session", "session_id": info.session_id,
                    "resume_token": info.resume_token,
                }))
                await _receive(again, "session_resumed")
                count = len(requests)
                # The displaced client wakes its consumer and stops.
                while await client.downlink_queue().get() is not None:
                    pass
                assert client.source_error == "The session was resumed on another connection"
                await asyncio.sleep(0.5)
                assert len(requests) == count  # no attempt to take it back
                with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                    await client.teardown()
                assert unconfirmed.value.session_id == info.session_id
                assert unconfirmed.value.resume_token is None
                assert server.sessions[info.session_id].ws is not None
                await again.send_str(json.dumps({"type": "teardown"}))
                await _receive(again, "closed")
            finally:
                await http2.close()
        finally:
            await http.close()
            if client is not None:
                await client.close()
            await server.stop()

    _run(scenario)


# Bugs caught: 0, False positives: 0
def test_a_stale_seek_without_a_target_is_ignored(source, tmp_path):
    async def scenario():
        server = RelayServer("models", free_port_pair(), library_root=str(tmp_path))
        await server.start()
        sockets = []
        try:
            http, ws = await _control(server.port)
            sockets.append(http)
            await ws.send_str(json.dumps({
                "type": "open_session", "source": {"type": "server_file", "path": "a.mkv"},
                "model": "passthrough", "quality_tier": "lossless-ffv1",
                "display": {"w": 64, "h": 64}, "resume": True,
            }))
            opened = await _receive(ws, "session_opened")
            await ws.send_str(json.dumps({"type": "seek", "epoch": 0}))  # stale, no target
            await ws.send_str(json.dumps({"type": "pause"}))
            assert (await _receive(ws, "state"))["state"] == "paused"
            assert server.sessions[opened["session_id"]].epoch == 0
        finally:
            for http in sockets:
                await http.close()
            await server.stop()

    _run(scenario)


async def _serve_control(handler):
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/control", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = free_port_pair()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


# Bugs caught: 0, False positives: 0
def test_a_connection_lost_right_after_session_opened_is_resumed(source):
    # session_opened and the end of the connection arrive back to back: the
    # token in that reply must still decide that the loss is resumed.
    from aiohttp import web
    attempts = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for raw in ws:
            msg = json.loads(raw.data)
            if msg["type"] == "hello":
                await ws.send_str(json.dumps({
                    "type": "capabilities", "models": [], "session_resume": True}))
            elif msg["type"] == "open_session":
                await ws.send_str(json.dumps({
                    "type": "session_opened", "session_id": "s1", "media_port": 1,
                    "uplink_token": "u" * 34, "downlink_token": "d" * 34,
                    "downlink_codec": "ffv1", "downlink_width": 64, "downlink_height": 64,
                    "resume_token": "t" * 34, "resume_grace_s": 1.0,
                }))
                await ws.close(code=1011)  # in the same breath
                return ws
            elif msg["type"] == "resume_session":
                attempts.append(msg["resume_token"])
                await ws.send_str(json.dumps({
                    "type": "error", "code": "resume_failed", "message": "gone",
                    "fatal": False}))
        return ws

    async def scenario():
        runner, port = await _serve_control(handler)
        client = RelayClient("127.0.0.1", port)
        try:
            await client.connect()
            events = []
            client.on_reconnecting = lambda: events.append("reconnecting")
            client.on_disconnected = lambda: events.append("disconnected")
            await client.open_session(_config(source))
            await _until(lambda: "disconnected" in events)
            assert events == ["reconnecting", "disconnected"]
            assert attempts == ["t" * 34]
        finally:
            await client.close()
            await runner.cleanup()

    _run(scenario)
