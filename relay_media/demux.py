"""Thread-safe video demux used by uplink clients and server-hosted media."""

from __future__ import annotations

import base64
import io
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterator

import av

from relay_protocol import FLAG_KEYFRAME, NO_TS, MediaPacket


_AUDIO_PREROLL_S = 0.25


@dataclass
class PacketInfo:
    payload: bytes
    pts: int
    dts: int
    keyframe: bool


@dataclass
class AuxiliaryPacketInfo:
    """One original audio/subtitle packet retained for stream-copy muxing.

    The packet itself is handed off intact so codec side data (notably audio
    skip samples and Matroska block additions) is not lost by serializing it
    through bytes. Ownership moves from the demux worker to the pipeline finish
    thread; no container or codec is shared between those threads.
    """

    packet: av.Packet
    stream_index: int
    order_s: float


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
            self._iter_gen += 1
            self._container.close()


class AuxiliaryTrack:
    """Seekable audio/subtitle demuxer for a server-hosted source.

    It deliberately owns a second input container. The video demuxer can be
    blocked or cancelled independently during an epoch change without two
    threads ever touching one native container. Source reads may therefore be
    duplicated, but only the compact selected packets are sent to the client.
    """

    def __init__(self, path: str):
        self.path = path
        self._container = av.open(path)
        self._streams = [
            stream for stream in self._container.streams
            if stream.type in ("audio", "subtitle")
        ]
        attachments = list(self._container.streams.attachments)
        self.attachment_bytes = sum(len(attachment.data) for attachment in attachments)
        self._lock = threading.Lock()
        self._iter_gen = 0
        try:
            self._validate_matroska_mux(attachments)
        except Exception:
            self._container.close()
            raise

    def _validate_matroska_mux(self, attachments: list) -> None:
        """Fail early when any source auxiliary codec cannot be remuxed.

        Server libraries also accept containers such as MP4. A codec like
        mov_text may be valid there but unsupported by Matroska; negotiation
        must downgrade that file to the external path before a pipeline/model
        is built, not fail playback minutes later.
        """
        if not self._streams and not attachments:
            return
        target = io.BytesIO()
        with av.open(target, "w", format="matroska") as output:
            for stream in self._streams:
                output.add_stream_from_template(stream)
            for attachment in attachments:
                metadata = dict(attachment.metadata)
                name = metadata.get("filename") or attachment.name or f"attachment-{attachment.index}"
                mimetype = attachment.mimetype or metadata.get("mimetype") or "application/octet-stream"
                output.add_attachment(name, mimetype, attachment.data)

    @property
    def has_streams(self) -> bool:
        return bool(self._streams)

    def packets(self, target_s: float | None = None) -> Iterator[AuxiliaryPacketInfo]:
        """Iterate original auxiliary packets, optionally from ``target_s``.

        A small audio preroll is retained and subtitle packets whose declared
        duration overlaps the target survive. mpv's initial audio sync trims
        samples before the first video PTS; keeping them is safer than starting
        codecs such as Opus/AAC without decoder preroll.
        """
        with self._lock:
            self._iter_gen = gen = self._iter_gen + 1
        return self._packet_iter(gen, target_s)

    def _packet_iter(
        self, gen: int, target_s: float | None,
    ) -> Iterator[AuxiliaryPacketInfo]:
        if not self._streams:
            return
        with self._lock:
            if self._iter_gen != gen:
                return
            if target_s is not None:
                # Anchor mixed demux on a dense audio index when available.
                # With no stream, libav may choose a sparse subtitle index and
                # next(iterator) then walks minutes of subtitle packets before
                # the timestamp merge can emit the first post-seek video.
                audio = next(
                    (stream for stream in self._streams if stream.type == "audio"),
                    None,
                )
                if audio is not None and audio.time_base is not None:
                    self._container.seek(
                        max(0, int(target_s / float(audio.time_base))),
                        stream=audio,
                        backward=True,
                        any_frame=False,
                    )
                else:
                    self._container.seek(
                        max(0, int(target_s * av.time_base)),
                        backward=True,
                        any_frame=False,
                    )
            iterator = self._container.demux(self._streams)
        while True:
            with self._lock:
                if self._iter_gen != gen:
                    return
                try:
                    packet = next(iterator)
                except StopIteration:
                    return
            if packet.pts is None and packet.dts is None and packet.size == 0:
                continue
            time_base = packet.time_base or packet.stream.time_base
            stamp = packet.dts if packet.dts is not None else packet.pts
            order_s = float(stamp * time_base) if stamp is not None and time_base else float("inf")
            if target_s is not None and packet.pts is not None and time_base is not None:
                start_s = float(packet.pts * time_base)
                duration_s = float((packet.duration or 0) * time_base)
                end_s = start_s + duration_s
                if packet.stream.type == "audio":
                    if start_s < target_s - _AUDIO_PREROLL_S and end_s <= target_s:
                        continue
                elif start_s < target_s and end_s <= target_s:
                    continue
            yield AuxiliaryPacketInfo(
                packet=packet,
                stream_index=packet.stream.index,
                order_s=order_s,
            )

    def close(self) -> None:
        with self._lock:
            self._iter_gen += 1
            self._container.close()
