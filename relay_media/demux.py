"""Thread-safe video demux used by uplink clients and server-hosted media."""

from __future__ import annotations

import base64
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator

import av

from relay_protocol import FLAG_KEYFRAME, NO_TS, MediaPacket


@dataclass
class PacketInfo:
    payload: bytes
    pts: int
    dts: int
    keyframe: bool


class VideoTrack:
    """Wrap a media file's first video stream for packet-level streaming."""

    def __init__(self, path: str):
        self.path = path
        self._container = av.open(path)
        self._stream = self._container.streams.video[0]
        # Serializes native demux/seek calls: a cancelled asyncio task's
        # in-flight to_thread(next, ...) keeps running on its worker thread,
        # and concurrent libav access is a native crash, not an exception.
        self._lock = threading.Lock()
        self._iter_gen = 0

    @property
    def time_base(self) -> Fraction:
        return self._stream.time_base

    @property
    def average_rate(self) -> Fraction | None:
        return self._stream.average_rate

    def open_session_video_dict(self) -> dict:
        cc = self._stream.codec_context
        extradata = bytes(cc.extradata) if cc.extradata else None
        avg = self._stream.average_rate
        return {
            "codec": cc.name,
            "extradata_b64": base64.b64encode(extradata).decode() if extradata else None,
            "width": cc.width,
            "height": cc.height,
            "time_base": [self.time_base.numerator, self.time_base.denominator],
            "avg_rate": [avg.numerator, avg.denominator] if avg else None,
        }

    def packets(self, from_pts: int | None = None) -> Iterator[PacketInfo]:
        """Iterate packets, optionally seeking to a keyframe before ``from_pts``.

        Each new iterator invalidates older iterators. This is load-bearing:
        cancelled ``asyncio.to_thread`` calls keep running and must not steal
        packets from a newer post-seek iterator that shares the container.
        """
        with self._lock:
            self._iter_gen = gen = self._iter_gen + 1
        return self._packet_iter(gen, from_pts)

    def _packet_iter(self, gen: int, from_pts: int | None) -> Iterator[PacketInfo]:
        with self._lock:
            if self._iter_gen != gen:
                return
            if from_pts is not None:
                self._container.seek(from_pts, stream=self._stream, backward=True, any_frame=False)
            iterator = self._container.demux(self._stream)
        while True:
            with self._lock:
                if self._iter_gen != gen:
                    return
                try:
                    packet = next(iterator)
                except StopIteration:
                    return
            if packet.pts is None and packet.size == 0:
                continue
            yield PacketInfo(
                payload=bytes(packet),
                pts=packet.pts if packet.pts is not None else NO_TS,
                dts=packet.dts if packet.dts is not None else NO_TS,
                keyframe=bool(packet.is_keyframe),
            )

    def media_packet(self, info: PacketInfo, epoch: int, discontinuity: bool = False) -> MediaPacket:
        flags = FLAG_KEYFRAME if info.keyframe else 0
        if discontinuity:
            from relay_protocol import FLAG_DISCONTINUITY

            flags |= FLAG_DISCONTINUITY
        return MediaPacket(payload=info.payload, flags=flags, epoch=epoch, pts=info.pts, dts=info.dts)

    def chapters(self) -> list[dict]:
        """Chapter list as wire-format dicts (docs/PROTOCOL.md session_opened).

        Each entry: {"start_s": float, "end_s": float | None, "title": str | None},
        sorted by start. Empty when the container has no chapters.
        """
        with self._lock:
            raw = self._container.chapters()
        chapters = []
        for chapter in raw:
            time_base = chapter.get("time_base")
            if time_base is None:
                continue
            start_s = float(chapter["start"] * time_base)
            end = chapter.get("end")
            end_s = float(end * time_base) if end is not None else None
            title = (chapter.get("metadata") or {}).get("title")
            chapters.append({
                "start_s": max(0.0, start_s),
                "end_s": end_s,
                "title": title or None,
            })
        chapters.sort(key=lambda c: c["start_s"])
        return chapters

    def duration_seconds(self) -> float | None:
        if self._stream.duration:
            return float(self._stream.duration * self.time_base)
        if self._container.duration:
            return self._container.duration / av.time_base
        return None

    def close(self) -> None:
        with self._lock:
            self._container.close()
