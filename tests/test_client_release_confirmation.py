"""After an unacknowledged teardown, /status says when the session is released.

A control connection that is already dead at teardown (suspend/resume, Wi-Fi
drop, server-side close) cannot carry the server's `closed` barrier. The
client names the session it could not confirm, and a replacement connection
polls the server's /status until that session is gone before anything may
open another one.
"""
import asyncio
import json
import threading

import pytest
from aiohttp import web

import relay_client_core.client as client_module
from relay_client_core import RelayClient, SessionConfig, TeardownNotConfirmedError
from relay_server.pipeline import Pipeline
from relay_server.server import RelayServer
from upscale_cli.sample import make_sample
from ports import free_port_pair

SESSION = "0123456789ab"
OTHER = "ba9876543210"


def _listing(*ids, **fields):
    return {"restart_required": False, "native_teardown_error": None,
            "sessions": [{"id": session_id, "state": "playing"} for session_id in ids], **fields}


class FakeServer:
    """/control that opens a session on request, and a scripted /status."""

    def __init__(self, answers=(), *, acknowledge=True, open_reply="session_opened"):
        self.answers = list(answers)  # the last one repeats
        self.polls = 0
        self.acknowledge = acknowledge
        self.open_reply = open_reply
        self.status_delay = 0.0
        self.port = free_port_pair()
        self.runner = None
        self._release = asyncio.Event()

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get("/control", self._control)
        app.router.add_get("/status", self._status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        return self

    async def __aexit__(self, *_exc):
        self._release.set()
        await self.runner.cleanup()

    async def _control(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for raw in ws:
            kind = json.loads(raw.data).get("type")
            if kind == "hello":
                await ws.send_str(json.dumps({"type": "capabilities", "models": []}))
            elif kind == "open_session" and self.open_reply == "session_opened":
                await ws.send_str(json.dumps({
                    "type": "session_opened", "session_id": SESSION, "media_port": self.port + 1,
                    "uplink_token": None, "downlink_token": "d" * 32, "downlink_codec": "hevc",
                    "downlink_width": 64, "downlink_height": 64, "source": "server_file",
                }))
            elif kind == "open_session" and self.open_reply == "restart_required":
                await ws.send_str(json.dumps({
                    "type": "error", "code": "server_restart_required",
                    "message": "restart the relay server", "fatal": True,
                }))
                break
            elif kind == "teardown" and self.acknowledge:
                await ws.send_str(json.dumps({"type": "closed"}))
        return ws

    async def _status(self, request):
        self.polls += 1
        if self.status_delay:
            try:
                await asyncio.wait_for(self._release.wait(), self.status_delay)
            except asyncio.TimeoutError:
                pass
        answer = self.answers[min(self.polls, len(self.answers)) - 1]
        if isinstance(answer, web.Response):
            return web.Response(status=answer.status, body=answer.body,
                                content_type=answer.content_type)
        return web.json_response(answer)


def _run(scenario):
    async def bounded():
        async with asyncio.timeout(30):
            await scenario()
    asyncio.run(bounded())


# -- the real server -----------------------------------------------------------


def test_session_dropped_without_teardown_is_confirmed_released(tmp_path, monkeypatch):
    source = tmp_path / "a.mkv"
    make_sample(str(source), frames=240, width=64, height=64, fps=10)
    allow_native_close = threading.Event()
    native_close_returned = threading.Event()
    close_pipeline = Pipeline.close

    def held_close(self, *args, **kwargs):
        # A native release takes a while; the session must stay listed for it.
        allow_native_close.wait(20)
        try:
            return close_pipeline(self, *args, **kwargs)
        finally:
            native_close_returned.set()

    monkeypatch.setattr(Pipeline, "close", held_close)

    async def scenario():
        server = RelayServer(str(tmp_path / "models"), free_port_pair())
        await server.start()
        lost = RelayClient("127.0.0.1", server.port)
        replacement = RelayClient("127.0.0.1", server.port)
        try:
            await lost.connect()
            session = await lost.open_session(SessionConfig(
                path=str(source), model="passthrough", quality_tier="lossless-ffv1",
                display_w=64, display_h=64,
            ))
            await lost.attach_media()
            await lost.start_uplink()
            await lost.play()
            assert await lost.downlink_queue().get() is not None
            # The id the client holds is the one /status lists.
            await replacement.connect()
            with pytest.raises(TeardownNotConfirmedError, match="still lists") as live:
                await replacement.wait_session_released(
                    session.session_id, timeout=0.2, interval=0.05)
            assert live.value.session_id == session.session_id

            # No close frame and no teardown message: the socket just goes away.
            lost._ws._conn.transport.abort()
            with pytest.raises(TeardownNotConfirmedError) as unconfirmed:
                await lost.teardown()
            assert unconfirmed.value.session_id == session.session_id

            released = asyncio.create_task(replacement.wait_session_released(
                session.session_id, timeout=20.0, interval=0.05))
            # Still listed for as long as the server's native close runs.
            await asyncio.sleep(0.4)
            assert not released.done() and not native_close_returned.is_set()
            allow_native_close.set()
            await released
            assert native_close_returned.is_set()
            assert session.session_id not in server.sessions
            assert server.native_teardown_error is None
            # The replacement connection may now open the next session.
            await replacement.open_session(SessionConfig(
                path=str(source), model="passthrough", quality_tier="lossless-ffv1",
                display_w=64, display_h=64,
            ))
            await replacement.teardown()
        finally:
            allow_native_close.set()
            await lost.close()
            await replacement.close()
            await server.stop()

    _run(scenario)


# -- a scripted /status ----------------------------------------------------------


def test_release_is_reported_on_the_first_poll_that_no_longer_lists_the_session():
    async def scenario():
        async with FakeServer([_listing(SESSION, OTHER)] * 3 + [_listing(OTHER)]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                loop = asyncio.get_running_loop()
                started = loop.time()
                await client.wait_session_released(SESSION, timeout=10.0, interval=0.1)
                assert server.polls == 4
                # Three more questions, each an interval after an answer.
                assert loop.time() - started >= 0.3
            finally:
                await client.close()

    _run(scenario)


def test_session_still_listed_at_the_deadline_is_not_confirmed():
    async def scenario():
        async with FakeServer([_listing(SESSION)]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                loop = asyncio.get_running_loop()
                started = loop.time()
                with pytest.raises(TeardownNotConfirmedError, match=SESSION) as stuck:
                    await client.wait_session_released(SESSION, timeout=0.3, interval=0.05)
                assert stuck.value.session_id == SESSION
                assert "still lists" in str(stuck.value)
                # The bound is checked after an answer, never instead of one.
                assert loop.time() - started >= 0.3
                assert server.polls >= 2
            finally:
                await client.close()

    _run(scenario)


@pytest.mark.parametrize("status", [
    # Checked first, whichever session failed and whether or not ours is listed.
    _listing(restart_required=True,
             native_teardown_error={"session_id": SESSION, "error": "timeout", "restart_required": True}),
    _listing(restart_required=True,
             native_teardown_error={"session_id": OTHER, "error": "timeout", "restart_required": True}),
    _listing(SESSION, restart_required=True),
])
def test_restart_required_is_never_confirmed(status):
    async def scenario():
        async with FakeServer([status]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                with pytest.raises(TeardownNotConfirmedError, match="restart the relay server") as failed:
                    await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
                assert failed.value.session_id == SESSION
                assert server.polls == 1
            finally:
                await client.close()

    _run(scenario)


@pytest.mark.parametrize("status", [
    {"sessions": []},  # a missing restart_required reads as false
    _listing(OTHER),
    # restart_required alone decides; the server sets both together.
    _listing(native_teardown_error={"session_id": SESSION, "error": "timeout"}),
])
def test_absent_session_without_restart_required_is_released(status):
    async def scenario():
        async with FakeServer([status]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                # The first question is asked at once, not an interval later.
                async with asyncio.timeout(5):
                    await client.wait_session_released(SESSION, timeout=60.0, interval=30.0)
                assert server.polls == 1
            finally:
                await client.close()

    _run(scenario)


@pytest.mark.parametrize("answer", [
    web.Response(status=500, text="boom"),
    web.Response(status=404, text="{}"),
    web.Response(status=302, headers={"Location": "/elsewhere"}),
    web.Response(text="<html>not the relay</html>", content_type="text/html"),
    web.Response(body=b"\xff\xfe\x00garbage", content_type="application/json"),
    web.Response(text="[]", content_type="application/json"),
    {"restart_required": False},  # no sessions list
    {"restart_required": False, "sessions": {"id": OTHER}},
    {"restart_required": False, "sessions": [{"state": "playing"}]},  # entry without id
    {"restart_required": False, "sessions": [{"id": OTHER}, {"id": 7}]},
    {"restart_required": False, "sessions": [OTHER]},
    {"restart_required": "no", "sessions": []},
    {"restart_required": None, "sessions": []},
])
def test_status_that_cannot_say_is_not_confirmed(answer):
    async def scenario():
        async with FakeServer([answer]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                with pytest.raises(TeardownNotConfirmedError, match="/status") as failed:
                    await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
                assert failed.value.session_id == SESSION
                assert server.polls == 1
            finally:
                await client.close()

    _run(scenario)


def test_unreachable_status_is_a_connection_failure_not_a_verdict():
    async def scenario():
        client = RelayClient("127.0.0.1", free_port_pair())  # nothing listens
        try:
            with pytest.raises(ConnectionError, match=SESSION):
                await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
        finally:
            await client.close()

    _run(scenario)


async def _raw_server(reply: bytes):
    """A listener that answers any request with these bytes and hangs up."""
    async def handler(reader, writer):
        await reader.read(4096)
        writer.write(reply)
        try:
            await writer.drain()
        finally:
            writer.close()

    port = free_port_pair()
    return await asyncio.start_server(handler, "127.0.0.1", port), port


def test_answer_that_is_not_http_is_not_confirmed():
    async def scenario():
        server, port = await _raw_server(b"\x00\x01 something else lives here\r\n\r\n")
        client = RelayClient("127.0.0.1", port)
        try:
            with pytest.raises(TeardownNotConfirmedError, match="/status") as failed:
                await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
            assert failed.value.session_id == SESSION
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    _run(scenario)


@pytest.mark.parametrize("reply", [
    b"",  # hung up without a word
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n{\"sess",
])
def test_connection_lost_mid_answer_is_a_connection_failure_not_a_verdict(reply):
    async def scenario():
        server, port = await _raw_server(reply)
        client = RelayClient("127.0.0.1", port)
        try:
            with pytest.raises(ConnectionError, match=SESSION):
                await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
        finally:
            await client.close()
            server.close()
            await server.wait_closed()

    _run(scenario)


def test_status_request_timeout_is_a_connection_failure_not_a_verdict():
    async def scenario():
        async with FakeServer([_listing()]) as server:
            server.status_delay = 5.0
            client = RelayClient("127.0.0.1", server.port)
            try:
                with pytest.raises(ConnectionError, match=SESSION):
                    await client.wait_session_released(
                        SESSION, timeout=5.0, interval=0.05, request_timeout=0.2)
                assert server.polls == 1
            finally:
                await client.close()

    _run(scenario)


def test_cancelling_the_wait_leaves_the_client_usable():
    async def scenario():
        async with FakeServer([_listing(SESSION)]) as server:
            client = RelayClient("127.0.0.1", server.port)
            try:
                await client.connect()
                wait = asyncio.create_task(
                    client.wait_session_released(SESSION, timeout=30.0, interval=0.05))
                while server.polls < 2:
                    await asyncio.sleep(0.01)
                wait.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await wait
                assert client.connected
                # Still the same question, still answerable on this client.
                server.answers = [_listing()]
                await client.wait_session_released(SESSION, timeout=5.0, interval=0.05)
            finally:
                await client.close()

    _run(scenario)


def test_default_bound_covers_the_servers_slowest_release():
    # ~30 s to notice a dead control connection plus a 15 s pipeline close,
    # with the session listed throughout.
    assert client_module.SESSION_RELEASE_TIMEOUT_S == 60.0
    assert client_module.SESSION_RELEASE_POLL_S == 1.0
    assert client_module.STATUS_REQUEST_TIMEOUT_S == 10.0
    assert client_module.SESSION_RELEASE_TIMEOUT_S > (
        client_module.CONTROL_HEARTBEAT_S * 1.5 + 15.0)


# -- which unacknowledged teardowns name a session ---------------------------------


async def _opened(server):
    client = RelayClient("127.0.0.1", server.port)
    await client.connect()
    await client.open_session(SessionConfig(
        path="a.mkv", model="passthrough", source="server_file"))
    return client


def test_acknowledged_teardown_raises_nothing():
    async def scenario():
        async with FakeServer() as server:
            client = await _opened(server)
            await client.teardown()

    _run(scenario)


def test_teardown_without_an_acknowledgement_names_the_session(monkeypatch):
    monkeypatch.setattr(client_module, "TEARDOWN_TIMEOUT_S", 0.2)

    async def scenario():
        async with FakeServer(acknowledge=False) as server:
            client = await _opened(server)
            with pytest.raises(TeardownNotConfirmedError, match="did not confirm") as failed:
                await client.teardown()
            assert failed.value.session_id == SESSION

    _run(scenario)


def test_teardown_before_the_session_id_arrived_names_no_session():
    async def scenario():
        async with FakeServer(open_reply="silence") as server:
            client = RelayClient("127.0.0.1", server.port)
            await client.connect()
            opening = asyncio.create_task(client.open_session(SessionConfig(
                path="a.mkv", model="passthrough", source="server_file")))
            while not client.has_server_session:
                await asyncio.sleep(0.01)
            client._ws._conn.transport.abort()
            with pytest.raises(ConnectionError):
                await opening
            with pytest.raises(TeardownNotConfirmedError) as failed:
                await client.teardown()
            assert failed.value.session_id is None

    _run(scenario)


def test_explicit_restart_required_names_no_session():
    async def scenario():
        async with FakeServer(open_reply="restart_required") as server:
            client = RelayClient("127.0.0.1", server.port)
            await client.connect()
            with pytest.raises(RuntimeError, match="server_restart_required"):
                await client.open_session(SessionConfig(
                    path="a.mkv", model="passthrough", source="server_file"))
            # Whatever id the client held: /status cannot clear this one.
            client.session = client_module.SessionInfo(
                session_id=SESSION, media_port=0, uplink_token=None, downlink_token="",
                downlink_codec="hevc", downlink_extradata=None,
                downlink_width=64, downlink_height=64)
            with pytest.raises(TeardownNotConfirmedError) as failed:
                await client.teardown()
            assert failed.value.session_id is None

    _run(scenario)


def test_local_owner_that_fails_to_close_names_no_session(monkeypatch):
    monkeypatch.setattr(client_module, "TEARDOWN_TIMEOUT_S", 0.2)

    async def scenario():
        async with FakeServer(acknowledge=False) as server:
            client = await _opened(server)

            class StuckTrack:
                def close(self):
                    raise OSError("mounted source did not close")

            client.track = StuckTrack()
            try:
                with pytest.raises(TeardownNotConfirmedError) as failed:
                    await client.teardown()
                assert failed.value.session_id is None
                assert isinstance(failed.value.__context__, OSError)
            finally:
                # The failed close stopped short of the sockets.
                await client._ws.close()
                await client._http.close()

    _run(scenario)
