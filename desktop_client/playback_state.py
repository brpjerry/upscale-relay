"""Cached playback state; none of these helpers reads native player properties."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class PlaybackSnapshot:
    source: str
    path: str
    position: float
    duration: float | None
    paused: bool
    tracks: dict[str, dict | None] = field(default_factory=dict)
    audio_delay: float = 0.0
    sub_delay: float = 0.0


@dataclass(frozen=True)
class PlaybackTelemetry:
    position: float | None
    duration: float | None
    buffered_ms: int
    mbps: float
    dropped: int
    drift: float
    stable: bool


def track_descriptor(track: dict, tracks: list[dict] | None = None) -> dict:
    descriptor = {key: track.get(key) for key in (
        "type", "lang", "title", "codec", "demux-channel-count", "default", "forced",
    )}
    if tracks is not None:
        occurrence = 0
        for candidate in tracks:
            if candidate is track or (candidate.get("type") == track.get("type") and candidate.get("id") == track.get("id")):
                break
            if track_descriptor(candidate) == descriptor:
                occurrence += 1
        descriptor["occurrence"] = occurrence
    return descriptor


def match_track(descriptor: dict, tracks: list[dict]) -> int | None:
    candidates = [t for t in tracks if t.get("type") == descriptor.get("type")]
    if not candidates:
        return None
    def score(t):
        return sum(weight for key, weight in (
            ("lang", 16), ("title", 8), ("codec", 4),
            ("demux-channel-count", 2), ("default", 1), ("forced", 1),
        ) if descriptor.get(key) is not None and t.get(key) == descriptor[key])
    best_score = max(map(score, candidates))
    matches = [t for t in candidates if score(t) == best_score]
    occurrence = descriptor.get("occurrence", 0)
    occurrence = occurrence if type(occurrence) is int and occurrence >= 0 else 0
    return matches[min(occurrence, len(matches) - 1)]["id"]


class Transitions:
    """Newest intent wins; native cleanup completes before another owner enters."""
    def __init__(self):
        self.generation = 0
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def run(self, operation: Callable[[], Awaitable]):
        self.generation += 1
        generation = self.generation
        previous = self._task
        if previous is not None and not previous.done() and not previous.cancelling():
            previous.cancel()
        async def execute():
            async with self._lock:
                if generation == self.generation:
                    return await operation()
        task = self._task = asyncio.create_task(execute())
        try:
            return await task
        except asyncio.CancelledError:
            if generation == self.generation:
                raise
            return None
