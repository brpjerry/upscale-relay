import asyncio
import hashlib
from pathlib import Path
import threading

import pytest

from relay_client_core import attachments as cache
from relay_client_core import client as client_module
from types import SimpleNamespace


def test_abandoned_hardlinked_views_do_not_pin_evicted_objects(tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
    entry = _font(b"font")
    (objects / entry["sha256"]).write_bytes(b"font")
    stale = tmp_path / "sessions" / "abandoned"
    stale.mkdir(parents=True)
    (stale / "font.ttf").hardlink_to(objects / entry["sha256"])
    (stale / ".lease").write_bytes(b"\0")
    view = cache._materialize_view(tmp_path, "new")
    try:
        assert not stale.exists()
        assert cache._acquire_cached(tmp_path, view, "font.ttf", entry)
        assert (view / "font.ttf").read_bytes() == b"font"
    finally:
        asyncio.run(cache.remove_attachment_view(view))


def test_live_views_survive_new_sessions_and_release_their_lease(tmp_path):
    (tmp_path / "objects").mkdir()
    first = cache._materialize_view(tmp_path, "same-session-id")
    second = cache._materialize_view(tmp_path, "same-session-id")
    try:
        assert first != second
        assert first.exists() and second.exists()
        assert first in cache._VIEW_LEASES
    finally:
        asyncio.run(cache.remove_attachment_view(first))
        asyncio.run(cache.remove_attachment_view(second))
    assert first not in cache._VIEW_LEASES
    assert not first.exists()


def test_failed_materialization_removes_partial_view_and_lease(tmp_path):
    async def scenario():
        with pytest.raises(ConnectionError):
            await cache.materialize_attachment_cache(
                _FontServer(b"font"), "http://server", "failed",
                [_font(b"font"), _font(b"absent", "absent.ttf")], "token", tmp_path,
            )
        assert list((tmp_path / "sessions").iterdir()) == []
        assert not any(path.is_relative_to(tmp_path) for path in cache._VIEW_LEASES)

    asyncio.run(scenario())


def test_cancelled_materialization_releases_the_late_view(tmp_path, monkeypatch):
    started, release, removed = threading.Event(), threading.Event(), threading.Event()
    original = cache._materialize_view
    original_remove = cache._remove_view

    def slow(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(cache, "_materialize_view", slow)
    def remove(path):
        original_remove(path)
        removed.set()
    monkeypatch.setattr(cache, "_remove_view", remove)

    async def scenario():
        task = asyncio.create_task(cache.materialize_attachment_cache(
            None, "http://server", "cancelled", [], "token", tmp_path))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # repeated cancellation cannot steal the worker's lease
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            release.set()
            assert await asyncio.to_thread(removed.wait, 2)
        finally:
            release.set()
        assert list((tmp_path / "sessions").iterdir()) == []
        assert not any(path.is_relative_to(tmp_path) for path in cache._VIEW_LEASES)

    asyncio.run(scenario())


def test_client_close_reaps_a_font_view_that_finishes_later(tmp_path, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_view(*args):
            started.set()
            await release.wait()
            return cache._materialize_view(tmp_path, "late")

        monkeypatch.setattr(client_module, "materialize_attachment_cache", delayed_view)
        client = client_module.RelayClient("localhost", 1)
        client.session = SimpleNamespace(
            aux_attachments="cached", attachment_token="token",
            session_id="late", attachment_manifest=[],
        )
        opening = asyncio.create_task(client.prepare_attachments(tmp_path))
        await started.wait()
        await client.close()
        release.set()
        with pytest.raises(ConnectionError, match="closed while preparing attachments"):
            await opening
        assert client._attachment_view_dir is None
        assert list((tmp_path / "sessions").iterdir()) == []
        assert not any(path.is_relative_to(tmp_path) for path in cache._VIEW_LEASES)

    asyncio.run(scenario())


def _font(data: bytes, name: str = "font.ttf") -> dict:
    return {
        "name": name, "mimetype": "font/ttf",
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }


class _FontServer:
    """Serve attachment bodies by hash; a gated body waits for its release."""

    def __init__(self, *fonts: bytes):
        self.bodies = {hashlib.sha256(data).hexdigest(): data for data in fonts}
        self.gates: dict[str, asyncio.Event] = {}
        self.waiting = asyncio.Event()

    def get(self, url, headers):
        return _FontResponse(self, url.rsplit("/", 1)[-1])


class _FontResponse:
    def __init__(self, server: _FontServer, digest: str):
        self.server, self.digest = server, digest
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.digest not in self.server.bodies:
            raise ConnectionError("attachment transfer failed")

    async def iter_chunked(self, _size):
        if (gate := self.server.gates.get(self.digest)) is not None:
            self.server.waiting.set()
            await gate.wait()
        yield self.server.bodies[self.digest]


def test_concurrent_open_cannot_evict_fonts_another_open_is_preparing(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 10)
    first, second, other = b"first!", b"second", b"other!"

    async def scenario():
        server = _FontServer(first, second, other)
        release = server.gates[_font(second)["sha256"]] = asyncio.Event()
        opening = asyncio.create_task(cache.materialize_attachment_cache(
            server, "http://server", "a",
            [_font(first, "first.ttf"), _font(second, "second.ttf")], "token", tmp_path,
        ))
        try:
            await server.waiting.wait()  # A holds its first font; B fills the cache
            view_b = await cache.materialize_attachment_cache(
                server, "http://server", "b", [_font(other)], "token", tmp_path,
            )
            await cache.remove_attachment_view(view_b)
            assert not (tmp_path / "objects" / _font(first)["sha256"]).exists()
            release.set()
            view_a = await opening
        finally:
            release.set()
        try:
            assert (view_a / "first.ttf").read_bytes() == first
            assert (view_a / "second.ttf").read_bytes() == second
        finally:
            await cache.remove_attachment_view(view_a)

    asyncio.run(scenario())


def test_corrupt_cached_object_is_fetched_again_and_replaced(tmp_path):
    entry = _font(b"font")
    (tmp_path / "objects").mkdir()
    (tmp_path / "objects" / entry["sha256"]).write_bytes(b"f0nt")

    async def scenario():
        view = await cache.materialize_attachment_cache(
            _FontServer(b"font"), "http://server", "s", [entry], "token", tmp_path)
        try:
            assert (view / "font.ttf").read_bytes() == b"font"
            assert sorted(path.name for path in view.iterdir()) == [".lease", "font.ttf"]
        finally:
            await cache.remove_attachment_view(view)

    asyncio.run(scenario())
    assert (tmp_path / "objects" / entry["sha256"]).read_bytes() == b"font"
