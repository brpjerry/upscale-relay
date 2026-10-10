"""GET /status is a client contract (docs/PROTOCOL.md "Status").

Clients poll it after losing the control connection to learn when their
session's native resources are released, and whether the server must be
restarted. They rely on one ordering: a session leaves ``sessions`` only
after its native close has returned, and a failed close is recorded in
``native_teardown_error`` / ``restart_required`` before that removal.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path

import aiohttp
import pytest

from ports import free_port_pair
from relay_client_core import RelayClient, SessionConfig
from relay_client_core.client import TeardownNotConfirmedError
from relay_server.pipeline import PIPELINE_CLOSE_TIMEOUT_S, Pipeline, PipelineCloseError
from relay_server.server import CONTROL_HEARTBEAT_S, RelayServer

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "_sample_stream.mkv"


@pytest.fixture(scope="module")
def sample_file() -> str:
    if not SAMPLE.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(SAMPLE), frames=240, width=320, height=180, fps=30)
    return str(SAMPLE)


class _Session:
    id = "session"
    uplink_token = "up"
    downlink_token = "down"
    pipeline = None

    def __init__(self, release: asyncio.Event, error: BaseException | None = None):
        self._release, self._error = release, error

    async def close(self):
        await self._release.wait()
        if self._error is not None:
            raise self._error


class _Ws:
    closed = True


def _bare_server(session) -> RelayServer:
    server = RelayServer.__new__(RelayServer)
    server.sessions = {"up": session, "down": session, "session": session}
    server.native_teardown_error = None
    return server


@pytest.mark.parametrize("failure", [None, PipelineCloseError("fake-finish survived")])
def test_cancelled_control_handler_keeps_the_session_listed_until_close_returns(failure):
    # Cancelling the control handler (server shutdown) does not stop the
    # shielded native close; the session must not vanish from /status first.
    async def scenario():
        release = asyncio.Event()
        session = _Session(release, failure)
        server = _bare_server(session)
        closing = asyncio.create_task(server._close_control_session(session, _Ws(), False))
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert server.sessions["session"] is session  # native close still running
        assert server.native_teardown_error is None
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert server.sessions == {}
        if failure is None:
            assert server.native_teardown_error is None
        else:
            assert server.native_teardown_error == {
                "session_id": "session", "error": repr(failure), "restart_required": True,
            }

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_status_after_an_abrupt_control_drop(sample_file, monkeypatch, fails):
    """What a client sees polling /status after its control connection died."""
    entered, release = threading.Event(), threading.Event()
    real_close = Pipeline.close

    def slow_close(self, *args, **kwargs):
        entered.set()
        assert release.wait(30)
        real_close(self, *args, **kwargs)
        if fails:
            raise PipelineCloseError("fake-finish survived")

    monkeypatch.setattr(Pipeline, "close", slow_close)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        seen = []
        try:
            await client.connect()
            info = await client.open_session(SessionConfig(
                path=sample_file, model="passthrough", quality_tier="lossless-ffv1",
                display_w=320, display_h=180,
            ))
            async with aiohttp.ClientSession() as http:
                async def status() -> dict:
                    async with http.get(f"http://127.0.0.1:{server.port}/status") as reply:
                        return await reply.json()

                def listed(report: dict) -> bool:
                    return info.session_id in [entry["id"] for entry in report["sessions"]]

                first = await status()
                assert listed(first) and first["restart_required"] is False
                assert first["native_teardown_error"] is None

                await client._ws.close()  # the control connection dies; no teardown
                assert await asyncio.to_thread(entered.wait, 10)
                for _ in range(5):  # the native close is in flight: still listed
                    report = await status()
                    assert listed(report) and report["restart_required"] is False
                    await asyncio.sleep(0.02)

                release.set()
                for _ in range(500):
                    report = await status()
                    seen.append((listed(report), report["restart_required"]))
                    if not listed(report):
                        break
                    await asyncio.sleep(0.01)
                assert not listed(report)
                # Once the session is gone its outcome is already published.
                assert report["restart_required"] is fails
                if fails:
                    assert report["native_teardown_error"]["session_id"] == info.session_id
                    assert report["native_teardown_error"]["restart_required"] is True
                    assert "fake-finish survived" in report["native_teardown_error"]["error"]
                else:
                    assert report["native_teardown_error"] is None
        finally:
            release.set()
            # The control connection was dropped on purpose: no acknowledgement.
            with contextlib.suppress(TeardownNotConfirmedError):
                await client.teardown()
            await server.stop()
        # Never "gone" without the failure being visible in the same response.
        assert all(still_listed or restart == fails for still_listed, restart in seen)

    asyncio.run(scenario())


# docs/PROTOCOL.md 1.1: how long clients poll GET /status for a session they
# could not get `closed` for, before giving up.
CLIENT_POLL_BOUND_S = 45.0


def test_client_poll_bound_covers_the_servers_timeouts():
    # aiohttp pings every heartbeat and waits half of it for the pong; then the
    # pipeline close has its own deadline. Raising either past the documented
    # bound would make healthy clients give up on a session that is closing.
    notice_dead_connection_s = CONTROL_HEARTBEAT_S * 1.5
    assert notice_dead_connection_s + PIPELINE_CLOSE_TIMEOUT_S <= CLIENT_POLL_BOUND_S


def test_restart_required_is_derived_from_the_recorded_teardown_error():
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        try:
            async with aiohttp.ClientSession() as http:
                async def status() -> dict:
                    async with http.get(f"http://127.0.0.1:{server.port}/status") as reply:
                        return await reply.json()

                clean = await status()
                assert clean["restart_required"] is False and clean["native_teardown_error"] is None
                assert clean["sessions"] == []
                server.native_teardown_error = {
                    "session_id": "abc", "error": "boom", "restart_required": True,
                }
                failed = await status()
                assert failed["restart_required"] is True
                assert failed["native_teardown_error"]["session_id"] == "abc"
        finally:
            await server.stop()

    asyncio.run(scenario())
