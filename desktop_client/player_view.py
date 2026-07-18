"""Playback view.

Interim implementation (prompt 3.4 will replace it with embedded libmpv):
decodes the downlink with PyAV on the asyncio loop and paints frames into a
QLabel, paced by PTS against a wall clock. Video-only — audio + real A/V sync
arrive with libmpv. The public surface (`start`, `stop`, signals) is what the
mpv-backed view will implement too.
"""

from __future__ import annotations

import asyncio
import time
from fractions import Fraction

import av
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from relay_protocol import NO_TS


class VideoPreviewView(QWidget):
    stats_changed = Signal(str)
    position_changed = Signal(float)
    track_list_changed = Signal(list)
    rebuffering = Signal(bool)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, parent=None, options=None):
        super().__init__(parent)
        self._label = QLabel("no session")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet("background:black; color:#888;")
        self._label.setMinimumSize(480, 270)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        self._task: asyncio.Task | None = None
        self._paused = False
        self.client = None  # set by MainWindow so buffered_ms can be reported

    # -- public API --------------------------------------------------------

    def start(self, session, downlink_q: asyncio.Queue, time_base: Fraction,
              source_path: str | None = None, avg_rate: Fraction | None = None) -> None:
        self.stop()
        if session.downlink_container is not None:
            # The downlink became a container stream (docs/PROTOCOL.md §3.2); this
            # interim decoder only understood raw packets. mpv is the real
            # backend — this view survives only as a clear error path.
            self.failed.emit(
                "preview backend cannot play container downlink; libmpv is required "
                "(place libmpv in mpv-dev/)"
            )
            return
        self._paused = False
        self._task = asyncio.create_task(self._consume(session, downlink_q, time_base))

    def select_subtitle(self, sid) -> None:
        pass

    def set_sub_delay(self, seconds: float) -> None:
        pass

    def play_local_fallback(self, position_s: float) -> None:
        self.failed.emit("local fallback requires the mpv backend")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._label.setText("no session")
        self._label.setPixmap(QPixmap())

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def set_panscan(self, value: float) -> None:
        pass  # no zoom/crop control in the interim preview backend

    def set_deband(self, enabled: bool) -> None:
        pass  # the PyAV preview has no GPU output pipeline

    # -- consumer ------------------------------------------------------------

    async def _consume(self, session, q: asyncio.Queue, time_base: Fraction) -> None:
        decoder = av.CodecContext.create(session.downlink_codec, "r")
        if session.downlink_extradata:
            decoder.extradata = session.downlink_extradata

        start_wall: float | None = None
        start_pts_s: float | None = None
        newest_pts_s = 0.0
        shown = 0
        try:
            while True:
                pkt = await q.get()
                if pkt is None:
                    self.failed.emit("downlink closed")
                    return
                if pkt.eos:
                    self.finished.emit()
                    return
                # Padded alloc + copy: Packet(bytes) wraps unpadded memory and
                # ffmpeg decoders overread past size (see relay_server.pipeline).
                av_pkt = av.Packet(len(pkt.payload))
                av_pkt.update(pkt.payload)
                av_pkt.pts = pkt.pts if pkt.pts != NO_TS else None
                av_pkt.dts = pkt.dts if pkt.dts != NO_TS else None
                for frame in decoder.decode(av_pkt):
                    if frame.pts is None:
                        continue
                    pts_s = float(frame.pts * time_base)
                    newest_pts_s = max(newest_pts_s, pts_s)
                    if start_wall is None:
                        start_wall, start_pts_s = time.monotonic(), pts_s
                    # Pace by PTS; pause holds the clock by shifting the origin.
                    while self._paused:
                        await asyncio.sleep(0.05)
                        start_wall = time.monotonic() - (pts_s - start_pts_s)
                    due = start_wall + (pts_s - start_pts_s)
                    delay = due - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self._show(frame)
                    shown += 1
                    if self.client is not None:
                        self.client.buffered_ms = max(0, int((newest_pts_s - pts_s) * 1000))
                    if shown % 15 == 0:
                        self.stats_changed.emit(
                            f"pos {pts_s:6.1f}s | buffered {int((newest_pts_s - pts_s) * 1000):5d} ms"
                            f" | {frame.width}x{frame.height}"
                        )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.failed.emit(f"playback: {err!r}")

    def _show(self, frame: av.VideoFrame) -> None:
        rgb = frame.to_ndarray(format="rgb24")
        h, w, _ = rgb.shape
        image = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        pix = QPixmap.fromImage(image).scaled(
            self._label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self._label.setPixmap(pix)
