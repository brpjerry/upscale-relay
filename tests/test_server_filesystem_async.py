import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from relay_media import AuxiliaryTrack, VideoTrack
from relay_server.library import LibraryPathError
from relay_server.server import RelayServer
from relay_server.session import Session


class _Ws:
    async def send_str(self, _message):
        pass


@pytest.mark.parametrize("operation", ["media_http", "open_session"])
def test_slow_library_resolution_does_not_hold_event_loop(operation):
    async def scenario():
        resolving, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        resolver_threads = []

        class Library:
            def resolve_file(self, _path):
                resolver_threads.append(threading.get_ident())
                resolving.set()
                if not release.wait(2):
                    raise RuntimeError("event loop did not release filesystem operation")
                if operation == "open_session":
                    raise LibraryPathError("unavailable share")
                return Path("source.mkv")

        if operation == "media_http":
            server = RelayServer("missing-models", 0)
            server.library = Library()
            work = asyncio.create_task(server.handle_media_file(SimpleNamespace(
                match_info={"path": "source.mkv"},
            )))
        else:
            session = Session(_Ws(), {}, library=Library())
            work = asyncio.create_task(session.handle_open({
                "source": {"type": "server_file", "path": "source.mkv"},
            }))
        try:
            assert await asyncio.to_thread(resolving.wait, 1)
            # Reaching here while resolution is still blocked proves all other
            # control/media tasks can continue to turn.
            assert not work.done()
            assert resolver_threads == [resolver_threads[0]]
            assert resolver_threads[0] != loop_thread
        finally:
            release.set()
            await work
            if operation == "open_session":
                await session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("track_type", [VideoTrack, AuxiliaryTrack])
def test_replacing_iterator_never_waits_for_native_demux_lock(track_type):
    track = track_type.__new__(track_type)
    track._lock = threading.Lock()
    track._generation_lock = threading.Lock()
    track._iter_gen = 0
    finished = threading.Event()
    result = []

    def replace():
        result.append(track.packets(7))
        finished.set()

    # Simulate a native file read which keeps running after task cancellation.
    with track._lock:
        worker = threading.Thread(target=replace, daemon=True)
        worker.start()
        progressed = finished.wait(0.5)
    worker.join(1)
    assert progressed
    assert track._iter_gen == 1
    assert len(result) == 1
