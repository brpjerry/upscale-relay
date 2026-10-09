"""Persistent policy and lifecycle invariants independent of native Qt playback."""
import asyncio
from dataclasses import asdict
import json

import pytest

from desktop_client.history import HistoryStore, HistoryEntry, source_key
from desktop_client.playback_state import Transitions, match_track, track_descriptor


class MemorySettings:
    def __init__(self):
        self.values = {}
    def value(self, key, default=None):
        return self.values.get(key, default)
    def setValue(self, key, value):
        self.values[key] = value
    def sync(self):
        pass


@pytest.mark.parametrize("position,duration,watched,resume", [
    (9.99, 1000, False, 0), (10, 1000, False, 10),
    (909.9, 1000, False, 909.9), (910, 1000, True, 0),
    (999, 1000, True, 0), (35, None, False, 35),
])
def test_history_resume_and_watched_boundaries(position, duration, watched, resume):
    settings = MemorySettings()
    history = HistoryStore(settings)
    history.save("local:/video.mkv", position, duration)
    reopened = HistoryStore(settings).entries["local:/video.mkv"]
    assert reopened.watched is watched
    assert reopened.resume == resume


def test_explicit_unknown_duration_watched_and_unwatched():
    history = HistoryStore(MemorySettings())
    history.save("local:/video", 400, None)
    history.mark("local:/video", True)
    history.save("local:/video", 401, None)
    assert history.entries["local:/video"].watched
    history.mark("local:/video", False)
    assert history.entries["local:/video"].resume == 0
    assert history.entries["local:/video"].position == 0


def test_history_retention_skips_invalid_records_individually():
    settings = MemorySettings()
    valid = asdict(HistoryEntry("local:/good", 20, 1000, False, 1234))
    settings.setValue("history/v1", json.dumps({"version": 1, "entries": [
        None, {}, {**valid, "position": "twenty"}, {**valid, "key": "local:/bad", "duration": -1},
        {**valid, "key": "local:/invalid-date", "last_played": 1e200}, valid,
    ]}))
    history = HistoryStore(settings, limit=2)
    assert list(history.entries) == ["local:/good"]
    history.save("local:/second", 20, 1000)
    history.save("local:/third", 20, 1000)
    assert set(HistoryStore(settings, limit=2).entries) == {"local:/second", "local:/third"}


def test_source_identity_normalizes_paths_and_keeps_endpoints_separate(tmp_path):
    assert source_key("local", str(tmp_path / "a/../b.mkv")) == source_key("uplink", str(tmp_path / "b.mkv"))
    assert source_key("server_file", "a/../b", "SERVER.local.", 8590) == source_key("server_file", "b", "server.local", 8590)
    assert source_key("server_file", "b", "server.local", 8590) != source_key("server_file", "b", "server.local", 9000)
    assert source_key("server_file", "b", "::1", 8590) == source_key("server_file", "b", "0:0:0:0:0:0:0:1", 8590)


def test_server_id_follows_the_server_across_addresses():
    key = lambda host, port, server_id: source_key("server_file", "Shows/a.mkv", host, port, server_id)
    assert key("192.168.0.115", 8590, "abc") == key("relay.local", 9000, "abc")
    assert key("relay.local", 8590, "abc") != key("relay.local", 8590, "def")
    # No (or an unusable) id: the address identifies the server, as before.
    for unusable in (None, "", 7, "a/b", "x" * 65, "caf\u00e9"):
        assert key("relay.local", 8590, unusable) == key("relay.local", 8590, None)
    assert key("relay.local", 8590, None) != key("relay.local", 8590, "abc")


def test_version_one_history_keeps_local_files_and_drops_server_files():
    settings = MemorySettings()
    local = asdict(HistoryEntry("local:/movie.mkv", 120, 1000, False, 1234))
    server = asdict(HistoryEntry("server:[relay.local]:8590/Shows/a.mkv", 300, 1400, False, 1235))
    settings.setValue("history/v1", json.dumps({"version": 1, "entries": [local, server]}))
    history = HistoryStore(settings)
    assert list(history.entries) == ["local:/movie.mkv"]
    keyed = source_key("server_file", "Shows/a.mkv", "relay.local", 8590, "abc")
    history.save(keyed, 300, 1400)
    reopened = HistoryStore(settings)
    assert set(reopened.entries) == {"local:/movie.mkv", keyed}
    assert json.loads(settings.value("history/v2"))["version"] == 2


def test_track_matching_remaps_ids_by_type_language_and_title():
    original = {"type": "sub", "id": 1, "lang": "eng", "title": "Signs", "codec": "ass"}
    tracks = [{**original, "id": 9}, {**original, "id": 1, "title": "Dialogue"},
              {**original, "id": 2, "type": "audio"}]
    assert match_track(track_descriptor(original), tracks) == 9
    assert match_track(track_descriptor(original), []) is None


def test_newest_transition_waits_for_cancelled_owner_cleanup():
    async def scenario():
        transitions = Transitions()
        started, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        async def old():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                events.append("closed")
        first = asyncio.create_task(transitions.run(old))
        await started.wait()
        async def next_op():
            events.append("opened")
        second = asyncio.create_task(transitions.run(next_op))
        await cleanup.wait()
        third = asyncio.create_task(transitions.run(next_op))
        await asyncio.sleep(0)
        assert events == []
        release.set()
        await asyncio.gather(first, second, third)
        assert events == ["closed", "opened"]
    asyncio.run(scenario())


def test_track_descriptor_distinguishes_duplicate_occurrences():
    tracks = [{"type": "audio", "id": 1, "lang": "en", "codec": "aac"},
              {"type": "audio", "id": 2, "lang": "en", "codec": "aac"}]
    descriptor = track_descriptor(tracks[1], tracks)
    assert descriptor["occurrence"] == 1
    assert match_track(descriptor, [{**tracks[0], "id": 6}, {**tracks[1], "id": 8}]) == 8
    assert match_track(descriptor, [{**tracks[0], "id": 6}]) == 6
