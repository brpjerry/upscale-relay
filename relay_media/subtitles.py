"""Temporary, bounded packet storage for subtitles overlapping a relay seek."""
from __future__ import annotations

import base64
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile

import av

MAX_SUBTITLE_INDEX_BYTES = 256 * 1024 * 1024
# Bitmap codecs can carry incremental display/clear state without packet
# durations. Keep their established external-media path until that state can
# be reconstructed, instead of pretending a duration index supports them.
INDEXABLE_SUBTITLE_CODECS = frozenset({"ass", "ssa", "subrip", "srt", "webvtt", "text"})


class SubtitleIndex:
    """Owned and serialized by AuxiliaryTrack's native demux lock.

    It stores original packet bodies/side data, never decoded subtitle text.
    SQLite's page cap bounds disk use; its page cache is limited to 2 MiB.
    No journal or persistent files survive this session.
    """

    def __init__(self, max_bytes: int = MAX_SUBTITLE_INDEX_BYTES):
        self.max_bytes = max_bytes
        self._cursors = set()
        self._directory = tempfile.TemporaryDirectory(prefix="relay-subtitles-")
        self.path = Path(self._directory.name) / "packets.sqlite"
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._db.execute("PRAGMA page_size=4096")
            self._db.execute(f"PRAGMA max_page_count={max(1, max_bytes // 4096)}")
            self._db.execute("PRAGMA journal_mode=OFF")
            self._db.execute("PRAGMA synchronous=OFF")
            self._db.execute("PRAGMA cache_size=-2048")
            self._db.execute("CREATE TABLE packets (identity BLOB PRIMARY KEY, stream INTEGER, "
                             "start REAL, end REAL, metadata TEXT, payload BLOB)")
            self._db.execute("CREATE INDEX end_times ON packets(end)")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def identity(packet: av.Packet) -> bytes:
        digest = hashlib.sha256()
        digest.update(str((packet.stream.index, packet.pos, packet.pts, packet.dts, packet.duration)).encode())
        digest.update(packet)
        return digest.digest()

    def remember(self, packet: av.Packet) -> None:
        if packet.pts is None or not packet.time_base or not packet.size:
            return
        if packet.size > self.max_bytes:
            raise RuntimeError("Temporary subtitle index packet exceeds its storage limit")
        start = float(packet.pts * packet.time_base)
        duration = packet.duration or 0
        if duration <= 0:
            # These text formats describe finite events; a zero-duration event
            # is not active after its timestamp, but its ordinary packet still
            # travels unchanged through the current demux iterator.
            return
        metadata = {
            "pts": packet.pts, "dts": packet.dts, "duration": duration,
            "tb": [packet.time_base.numerator, packet.time_base.denominator],
            "keyframe": packet.is_keyframe, "corrupt": packet.is_corrupt,
            "side": [(side.data_type, base64.b64encode(bytes(side)).decode("ascii"))
                     for side in packet.iter_sidedata()],
        }
        try:
            self._db.execute("INSERT OR IGNORE INTO packets VALUES (?, ?, ?, ?, ?, ?)", (
                self.identity(packet), packet.stream.index, start,
                start + float(duration * packet.time_base), json.dumps(metadata), bytes(packet),
            ))
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise RuntimeError(
                    "Temporary subtitle index exceeded its 256 MiB limit; "
                    "reopen this file with external auxiliary tracks"
                ) from error
            raise

    def overlapping(self, target_s: float):
        cursor = self._db.execute(
            "SELECT identity, stream, metadata, payload FROM packets "
            "WHERE start < ? AND end > ? ORDER BY start, stream, identity",
            (target_s, target_s),
        )
        self._cursors.add(cursor)
        return cursor

    def close_cursor(self, cursor) -> None:
        if cursor in self._cursors:
            self._cursors.remove(cursor)
            cursor.close()

    @staticmethod
    def restore(row, streams) -> tuple[bytes, av.Packet]:
        identity, stream_index, metadata, payload = row
        metadata = json.loads(metadata)
        packet = av.Packet(len(payload))
        packet.update(payload)
        packet.stream = streams[stream_index]
        packet.pts, packet.dts = metadata["pts"], metadata["dts"]
        packet.duration = metadata["duration"]
        packet.time_base = Fraction(*metadata["tb"])
        packet.is_keyframe = metadata["keyframe"]
        packet.is_corrupt = metadata["corrupt"]
        for name, encoded in metadata["side"]:
            data = base64.b64decode(encoded)
            side = av.packet.PacketSideData(av.packet.packet_sidedata_type_from_literal(name), len(data))
            side.update(data)
            packet.set_sidedata(side)
        return identity, packet

    def close(self) -> None:
        database = getattr(self, "_db", None)
        try:
            if database is not None:
                # Finalize replay statements before closing SQLite. Otherwise
                # sqlite3_close_v2 can retain the file handle until a suspended
                # generator resumes, preventing temporary-file deletion on Windows.
                for cursor in list(self._cursors):
                    self.close_cursor(cursor)
                database.close()
                self._db = None
        finally:
            self._directory.cleanup()
