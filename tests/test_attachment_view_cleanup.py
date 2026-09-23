import asyncio
from pathlib import Path
import threading

import pytest

from relay_client_core import attachments as cache


def test_abandoned_hardlinked_views_do_not_pin_evicted_objects(tmp_path):
    objects = tmp_path / "objects"
    objects.mkdir()
    digest = "a" * 64
    (objects / digest).write_bytes(b"font")
    stale = tmp_path / "sessions" / "abandoned"
    stale.mkdir(parents=True)
    (stale / "font.ttf").hardlink_to(objects / digest)
    (stale / ".lease").write_bytes(b"\0")
    view = cache._materialize_view(tmp_path, "new", [{"name": "font.ttf", "sha256": digest}])
    try:
        assert not stale.exists()
        assert (view / "font.ttf").read_bytes() == b"font"
    finally:
        asyncio.run(cache.remove_attachment_view(view))


def test_live_views_survive_new_sessions_and_release_their_lease(tmp_path):
    (tmp_path / "objects").mkdir()
    first = cache._materialize_view(tmp_path, "same-session-id", [])
    second = cache._materialize_view(tmp_path, "same-session-id", [])
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
    with pytest.raises(FileNotFoundError):
        cache._materialize_view(tmp_path, "failed", [{"name": "font.ttf", "sha256": "absent"}])
    assert list((tmp_path / "sessions").iterdir()) == []
    assert not any(path.is_relative_to(tmp_path) for path in cache._VIEW_LEASES)


def test_cancelled_materialization_releases_the_late_view(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = cache._materialize_view

    def slow(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(cache, "_materialize_view", slow)

    async def scenario():
        task = asyncio.create_task(cache.materialize_attachment_cache(
            None, "http://server", "cancelled", [], "token", tmp_path))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        assert list((tmp_path / "sessions").iterdir()) == []
        assert not any(path.is_relative_to(tmp_path) for path in cache._VIEW_LEASES)

    asyncio.run(scenario())
