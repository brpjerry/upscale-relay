"""Session: state machine + glue between control WS, media sockets, pipeline."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from enum import Enum
from fractions import Fraction
from typing import Any

from relay_protocol import FLAG_EOS, MediaPacket, new_token
from relay_media import VideoTrack
from upscale_cli.encode import DEFAULT_LOSSLESS_HEVC_PROFILE
from upscale_cli.fit import DEFAULT_RESIZE_ALGORITHM, RESIZE_ALGORITHMS

from .library import MediaLibrary
from .pipeline import Pipeline, VideoConfig

log = logging.getLogger("relay.session")


class State(str, Enum):
    OPEN = "open"
    PLAYING = "playing"
    PAUSED = "paused"
    CLOSED = "closed"


class Session:
    def __init__(self, ws, models: dict[str, str], ep: str = "auto",
                 library: MediaLibrary | None = None,
                 default_resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM,
                 lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE):
        self.id = uuid.uuid4().hex[:12]
        self.ws = ws
        self.models = models  # name -> path
        self.ep = ep
        self.library = library
        self.default_resize_algorithm = default_resize_algorithm
        self.lossless_hevc_profile = lossless_hevc_profile
        self.source_kind = "uplink"
        self.source_path: str | None = None
        self.source_track: VideoTrack | None = None
        self._source_task: asyncio.Task | None = None
        self.state = State.OPEN
        self.epoch = 0
        self.uplink_token = new_token()
        self.downlink_token = new_token()
        self.pipeline: Pipeline | None = None
        self.down_q: asyncio.Queue[MediaPacket | None] = asyncio.Queue(maxsize=512)
        self.uplink_attached = False
        self.downlink_attached = False
        self.last_buffer_report = time.monotonic()
        self.created = time.monotonic()
        self._loop = asyncio.get_running_loop()

    # -- helpers ---------------------------------------------------------------

    async def send(self, type_: str, **fields: Any) -> None:
        try:
            await self.ws.send_str(json.dumps({"type": type_, **fields}))
        except ConnectionResetError:
            pass

    async def set_state(self, state: State) -> None:
        if self.state == state:
            return
        self.state = state
        if self.pipeline:
            self.pipeline.playing = state == State.PLAYING
        log.info("session %s -> %s", self.id, state.value)
        await self.send("state", state=state.value)

    def _emit_downlink(self, pkt: MediaPacket) -> None:
        """Called from the pipeline worker thread. Blocks that thread while
        the downlink writer is behind (real backpressure — the old queue
        expansion ballooned memory when the client drained slowly)."""
        import time as _time

        waited = 0.0
        while self.down_q.qsize() >= 256 and self.state != State.CLOSED:
            _time.sleep(0.05)
            waited += 0.05
            if waited >= 30.0:
                log.warning("session %s: downlink stalled >30s, dropping session", self.id)
                asyncio.run_coroutine_threadsafe(self.close(), self._loop)
                return

        def _put() -> None:
            if pkt.epoch < self.epoch:
                return  # a seek happened while this was in flight
            try:
                self.down_q.put_nowait(pkt)
            except asyncio.QueueFull:
                pass  # bounded by the gate above; only a burst race lands here

        self._loop.call_soon_threadsafe(_put)

    def _pipeline_error(self, message: str) -> None:
        async def _report() -> None:
            await self.send("error", code="pipeline_error", message=message, fatal=True)
            await self.close()

        asyncio.run_coroutine_threadsafe(_report(), self._loop)

    # -- control message handlers ----------------------------------------------

    async def handle_open(self, msg: dict) -> None:
        source = msg.get("source", "uplink")
        if isinstance(source, dict):
            source_kind = source.get("type")
        else:
            source_kind = source
        if source_kind not in ("uplink", "server_file"):
            await self.send("error", code="bad_message", message="invalid source", fatal=False)
            return
        self.source_kind = source_kind
        if source_kind == "server_file":
            if self.library is None:
                await self.send("error", code="bad_message", message="server has no library", fatal=False)
                return
            relative = source.get("path") if isinstance(source, dict) else None
            try:
                resolved = self.library.resolve_file(relative or "")
                self.source_track = await asyncio.to_thread(VideoTrack, str(resolved))
            except Exception as err:
                await self.send("error", code="decode_error", message=str(err), fatal=False)
                return
            self.source_path = relative
            video = self.source_track.open_session_video_dict()
        else:
            video = msg.get("video")
            if not isinstance(video, dict):
                await self.send("error", code="bad_message", message="missing video", fatal=False)
                return
        model_name = msg.get("model") or "passthrough"
        if model_name not in ("passthrough", *self.models):
            await self.send("error", code="unknown_model", message=model_name, fatal=False)
            return
        display = msg.get("display") or {"w": video["width"], "h": video["height"]}
        fit_mode = msg.get("fit_mode", "fit")
        if fit_mode not in ("fit", "cover"):
            await self.send("error", code="bad_message", message="invalid fit_mode", fatal=False)
            return
        resize_algorithm = msg.get("resize_algorithm", self.default_resize_algorithm)
        if resize_algorithm not in RESIZE_ALGORITHMS:
            await self.send(
                "error", code="unknown_resize_algorithm",
                message=str(resize_algorithm), fatal=False,
            )
            return
        cfg = VideoConfig(
            codec=video["codec"],
            extradata=base64.b64decode(video["extradata_b64"]) if video.get("extradata_b64") else None,
            width=video["width"],
            height=video["height"],
            time_base=Fraction(*video["time_base"]),
            avg_rate=Fraction(*video["avg_rate"]) if video.get("avg_rate") else None,
        )
        try:
            self.pipeline = await asyncio.to_thread(
                Pipeline,
                cfg,
                self.models.get(model_name),
                msg.get("quality_tier", "lossless-hevc"),
                (display["w"], display["h"]),
                self._emit_downlink,
                self._pipeline_error,
                self.ep,
                fit_mode=fit_mode,
                resize_algorithm=resize_algorithm,
                lossless_hevc_profile=self.lossless_hevc_profile,
            )
        except Exception as err:
            await self.send("error", code="pipeline_error", message=str(err), fatal=True)
            await self.close()
            return
        duration_s = (self.source_track.duration_seconds() if self.source_track else
                      (msg.get("file") or {}).get("duration_s"))
        avg_rate = self.source_track.average_rate if self.source_track else cfg.avg_rate
        await self.send(
            "session_opened",
            session_id=self.id,
            media_port=self.media_port,
            uplink_token=self.uplink_token if self.source_kind == "uplink" else None,
            downlink_token=self.downlink_token,
            epoch=0,
            downlink_container=self.pipeline.downlink_container,
            downlink_codec=self.pipeline.downlink_codec,
            downlink_extradata_b64=self.pipeline.downlink_extradata_b64,
            downlink_width=self.pipeline.out_w,
            downlink_height=self.pipeline.out_h,
            fit_mode=self.pipeline.fit_mode,
            resize_algorithm=self.pipeline.resize_algorithm,
            source=self.source_kind,
            time_base=[cfg.time_base.numerator, cfg.time_base.denominator],
            duration_s=duration_s,
            avg_rate=[avg_rate.numerator, avg_rate.denominator] if avg_rate else None,
        )

    media_port: int = 0  # set by server at construction

    async def handle_seek(self, msg: dict) -> None:
        new_epoch = int(msg["epoch"])
        if new_epoch <= self.epoch or self.pipeline is None:
            return  # stale or premature
        self.epoch = new_epoch
        # Drop everything queued for the downlink writer.
        try:
            while True:
                self.down_q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self.pipeline.note_buffer_report(0)
        await self._stop_server_source()
        await asyncio.to_thread(self.pipeline.flush, new_epoch, int(msg["target_pts"]))
        if self.downlink_attached:
            await self.start_server_source(int(msg["target_pts"]), discontinuity=True)
        await self.send("seek_ready", epoch=new_epoch)

    async def start_server_source(self, from_pts: int | None = None,
                                  discontinuity: bool = False) -> None:
        if self.source_track is None or self.pipeline is None:
            return
        if self._source_task is not None and not self._source_task.done():
            return
        epoch = self.epoch
        self._source_task = asyncio.create_task(
            self._server_source_loop(from_pts, discontinuity, epoch)
        )

    async def _stop_server_source(self) -> None:
        if self._source_task is None:
            return
        self._source_task.cancel()
        await asyncio.gather(self._source_task, return_exceptions=True)
        self._source_task = None

    async def _server_source_loop(self, from_pts: int | None,
                                  discontinuity: bool, epoch: int) -> None:
        assert self.source_track is not None and self.pipeline is not None
        iterator = self.source_track.packets(from_pts)
        first = True

        def next_batch() -> list:
            batch = []
            for info in iterator:
                batch.append(info)
                if len(batch) >= 16:
                    break
            return batch

        try:
            while epoch == self.epoch and self.state != State.CLOSED:
                batch = await asyncio.to_thread(next_batch)
                if epoch != self.epoch:
                    return
                for info in batch:
                    pkt = self.source_track.media_packet(
                        info, epoch, discontinuity=discontinuity and first
                    )
                    first = False
                    await asyncio.to_thread(self.pipeline.feed, pkt)
                if len(batch) < 16:
                    await asyncio.to_thread(
                        self.pipeline.feed,
                        MediaPacket(payload=b"", flags=FLAG_EOS, epoch=epoch),
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._pipeline_error(str(err))

    def handle_buffer_report(self, msg: dict) -> None:
        self.last_buffer_report = time.monotonic()
        if self.pipeline:
            self.pipeline.note_buffer_report(int(msg.get("buffered_ms", 0)))

    async def close(self) -> None:
        if self.state == State.CLOSED:
            return
        await self.set_state(State.CLOSED)
        await self._stop_server_source()
        if self.pipeline:
            self.pipeline.close()
        if self.source_track is not None:
            await asyncio.to_thread(self.source_track.close)
            self.source_track = None
        # Wake the downlink writer so it exits.
        try:
            self.down_q.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def status(self) -> dict:
        p = self.pipeline
        return {
            "id": self.id,
            "state": self.state.value,
            "epoch": self.epoch,
            "uplink_attached": self.uplink_attached,
            "downlink_attached": self.downlink_attached,
            "source": self.source_kind,
            "pipeline": None
            if p is None
            else {
                "frames_in": p.stats.frames_in,
                "frames_out": p.stats.frames_out,
                "fps": round(p.stats.fps, 2),
                "in_queue": p.in_q.qsize(),
                "down_queue": self.down_q.qsize(),
                "paused_for_backpressure": p.stats.paused_for_backpressure,
                "stage_ms": p.stats.stage_report(),
                "client_buffered_ms": p.client_buffered_ms,
                "client_buffered_ms_est": round(p.buffered_ms_now()),
                "output": f"{p.out_w}x{p.out_h}",
                "codec": p.downlink_codec,
                "encoder": p.encoder_name,
                "quality_tier": p.quality_tier,
                "lossless_hevc_profile": (
                    p.lossless_hevc_profile if p.quality_tier == "lossless-hevc" else None
                ),
                "fit_mode": p.fit_mode,
                "resize_algorithm": p.resize_algorithm,
            },
        }
