"""Session: state machine + glue between control WS, media sockets, pipeline."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import time
import uuid
from enum import Enum
from fractions import Fraction
from typing import Any

from relay_protocol import DIR_DOWNLINK, DIR_UPLINK, FLAG_EOS, NO_TS, MediaPacket, new_token
from relay_media import AuxiliaryTrack, VideoTrack
from upscale_cli.encode import DEFAULT_LOSSLESS_HEVC_PROFILE
from upscale_cli.fit import DEFAULT_RESIZE_ALGORITHM, RESIZE_ALGORITHMS

from .library import MediaLibrary
from .pipeline import Pipeline, PipelineConstructionError, VideoConfig

log = logging.getLogger("relay.session")

# open_session keepalive pacing: a quick open sends nothing (initial delay
# never elapses); a slow one — a first-use TensorRT engine build runs for
# minutes — ticks session_progress so clients can hold their timeout open and
# show a loading indicator. Module-level so tests can shrink them.
PROGRESS_INITIAL_DELAY_S = 2.0
PROGRESS_INTERVAL_S = 2.0

# seek_progress pacing (docs/PROTOCOL.md, docs/SEEK_LATENCY_PLAN.md step 4).
# A seek that produces its first packet promptly sends nothing; a slow one —
# a long keyframe-to-target discard window — ticks so the client can show real
# progress instead of a spinner over a stale buffer readout.
SEEK_PROGRESS_INITIAL_DELAY_S = 0.75
SEEK_PROGRESS_INTERVAL_S = 0.5
# Give up narrating eventually: a seek past the last keyframe produces no
# surviving frame at all, and silently ticking forever would hide that.
SEEK_PROGRESS_MAX_S = 60.0

# Legacy clients embed attachments in the first Matroska payload of every
# epoch. Above this bound, preserve fonts through /media rather than repeat a
# huge header. Cache-capable clients negotiate verified font objects and do not
# apply this limit because epoch containers omit their bodies.
MAX_MUXED_ATTACHMENT_BYTES = 4 * 1024 * 1024

_MAX_CHAPTERS = 512


def _sanitize_chapters(raw: Any) -> list[dict]:
    """Validate client-supplied file.chapters before echoing them back."""
    if not isinstance(raw, list):
        return []
    chapters = []
    for item in raw[:_MAX_CHAPTERS]:
        if not isinstance(item, dict):
            continue
        start_s = item.get("start_s")
        if not isinstance(start_s, (int, float)) or start_s < 0:
            continue
        end_s = item.get("end_s")
        title = item.get("title")
        chapters.append({
            "start_s": float(start_s),
            "end_s": float(end_s) if isinstance(end_s, (int, float)) else None,
            "title": str(title) if title is not None else None,
        })
    chapters.sort(key=lambda c: c["start_s"])
    return chapters


class State(str, Enum):
    OPEN = "open"
    PLAYING = "playing"
    PAUSED = "paused"
    CLOSED = "closed"


class _DownlinkQueue(asyncio.Queue):
    """Bound both packet count and payload bytes on the owning event loop."""

    def __init__(self, maxsize: int = 256, max_bytes: int = 128 * 1024 * 1024):
        super().__init__(maxsize=maxsize)
        self.max_bytes = max_bytes
        self.payload_bytes = 0
        self._capacity = asyncio.Event()
        self._retired = False

    def retire(self) -> None:
        self._retired = True
        self._capacity.set()

    def _check_live(self, item: MediaPacket | None) -> None:
        if item is not None and self._retired:
            raise asyncio.CancelledError

    @staticmethod
    def _size(item: MediaPacket | None) -> int:
        return len(item.payload) if item is not None else 0

    def _fits(self, item: MediaPacket | None) -> bool:
        return not self.full() and self.payload_bytes + self._size(item) <= self.max_bytes

    async def put(self, item: MediaPacket | None) -> None:
        self._check_live(item)
        if self._size(item) > self.max_bytes:
            raise ValueError("downlink packet exceeds queue byte budget")
        while not self._fits(item):
            self._capacity.clear()
            await self._capacity.wait()
            self._check_live(item)
        self.put_nowait(item)

    def put_nowait(self, item: MediaPacket | None) -> None:
        self._check_live(item)
        if not self._fits(item):
            raise asyncio.QueueFull
        super().put_nowait(item)
        self.payload_bytes += self._size(item)

    def get_nowait(self) -> MediaPacket | None:
        item = super().get_nowait()
        self.payload_bytes -= self._size(item)
        self._capacity.set()
        return item


class Session:
    def __init__(self, ws, models: dict[str, str], ep: str = "auto",
                 library: MediaLibrary | None = None,
                 default_resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM,
                 lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE,
                 seek_discard_max_s: float | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.ws = ws
        self.models = models  # name -> path
        self.ep = ep
        self.library = library
        self.default_resize_algorithm = default_resize_algorithm
        self.lossless_hevc_profile = lossless_hevc_profile
        self.seek_discard_max_s = seek_discard_max_s
        self.source_kind = "uplink"
        self.source_path: str | None = None
        self.source_track: VideoTrack | None = None
        self.aux_track: AuxiliaryTrack | None = None
        self.aux_track_mode = "external"
        self.aux_attachment_mode = "embedded"
        self.attachment_token: str | None = None
        self.attachment_manifest: list[dict] = []
        self._source_task: asyncio.Task | None = None
        self._open_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._seek_progress_task: asyncio.Task | None = None
        self.state = State.OPEN
        self.epoch = 0
        self.uplink_token = new_token()
        self.downlink_token = new_token()
        self.pipeline: Pipeline | None = None
        self.down_q: asyncio.Queue[MediaPacket | None] = _DownlinkQueue()
        self._media_connections: dict[int, tuple[asyncio.StreamWriter, asyncio.Task]] = {}
        self.uplink_attached = False
        self.downlink_attached = False
        self.last_buffer_report = time.monotonic()
        self.created = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._final_pipeline_status: dict | None = None

    # -- helpers ---------------------------------------------------------------

    def register_media(self, direction: int, writer: asyncio.StreamWriter) -> bool:
        """Reserve exactly one live attachment per direction before accepting it."""
        if self.state == State.CLOSED or self._close_task is not None:
            return False
        if direction in self._media_connections:
            return False
        self._media_connections[direction] = (writer, asyncio.current_task())
        self.uplink_attached = DIR_UPLINK in self._media_connections
        self.downlink_attached = DIR_DOWNLINK in self._media_connections
        return True

    def unregister_media(self, direction: int, writer: asyncio.StreamWriter) -> None:
        current = self._media_connections.get(direction)
        if current is not None and current[0] is writer:
            self._media_connections.pop(direction)
        self.uplink_attached = DIR_UPLINK in self._media_connections
        self.downlink_attached = DIR_DOWNLINK in self._media_connections

    async def _close_media(self, initiator: asyncio.Task | None) -> None:
        connections = list(self._media_connections.values())
        tasks = []
        for writer, task in connections:
            # Teardown abandons pending epoch data. Graceful close alone can
            # wait forever for a peer which has stopped reading a full socket.
            writer.close()
            writer.transport.abort()
            if task is not initiator and task is not asyncio.current_task():
                task.cancel()
                tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for writer, _task in connections:
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass
        self._media_connections.clear()
        self.uplink_attached = self.downlink_attached = False

    async def send(self, type_: str, **fields: Any) -> None:
        try:
            await self.ws.send_str(json.dumps({"type": type_, **fields}))
        except (ConnectionResetError, RuntimeError):
            # RuntimeError: aiohttp's "closing transport" — reachable now that
            # a backgrounded open can outlive its WS.
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
        """Apply backpressure to the complete worker-to-event-loop handoff.

        Waiting for Queue.put also bounds callbacks waiting for the event loop.
        A qsize check followed by call_soon could queue unbounded callbacks and
        then silently lose Matroska bytes when those callbacks filled the queue.
        """
        if self.state == State.CLOSED or pkt.epoch < self.epoch:
            return
        pending = self.down_q.put(pkt)
        try:
            delivery = asyncio.run_coroutine_threadsafe(pending, self._loop)
        except RuntimeError:
            pending.close()
            return  # the owning event loop has already shut down
        deadline = time.monotonic() + 30.0
        try:
            while self.state != State.CLOSED and pkt.epoch >= self.epoch:
                try:
                    delivery.result(timeout=0.1)
                    return
                except concurrent.futures.TimeoutError:
                    if time.monotonic() >= deadline:
                        log.warning("session %s: downlink stalled >30s, dropping session", self.id)
                        asyncio.run_coroutine_threadsafe(self.close(), self._loop)
                        return
                except concurrent.futures.CancelledError:
                    return
        finally:
            # In particular, do not leave a stale seek or closed-session put
            # waiting behind the current epoch's media.
            if not delivery.done():
                delivery.cancel()

    async def _progress_keepalive(self, stage: str, message: str,
                                  done: asyncio.Event) -> None:
        """Tick session_progress until ``done`` while a slow open runs."""
        started = time.monotonic()
        try:
            await asyncio.wait_for(done.wait(), PROGRESS_INITIAL_DELAY_S)
            return  # quick open: stay silent
        except asyncio.TimeoutError:
            pass
        while not done.is_set():
            await self.send(
                "session_progress", stage=stage, message=message,
                elapsed_s=round(time.monotonic() - started, 1),
            )
            try:
                await asyncio.wait_for(done.wait(), PROGRESS_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    def _pipeline_error(self, message: str) -> None:
        async def _report() -> None:
            await self.send("error", code="pipeline_error", message=message, fatal=True)
            await self.close()

        asyncio.run_coroutine_threadsafe(_report(), self._loop)

    # -- control message handlers ----------------------------------------------

    def begin_open(self, msg: dict) -> None:
        """Run handle_open as a task so the control WS keeps servicing pings
        while a slow pipeline build (first-use TensorRT engine) runs."""
        self._open_task = asyncio.create_task(self._open_and_reap(msg))

    async def _open_and_reap(self, msg: dict) -> None:
        try:
            await self.handle_open(msg)
        except Exception:
            log.exception("session %s: open failed", self.id)
        finally:
            # The WS may have died while the engine was building; close() ran
            # with pipeline still None, so dispose the late arrival here.
            if self.state == State.CLOSED and self.pipeline is not None:
                pipeline, self.pipeline = self.pipeline, None
                await asyncio.to_thread(pipeline.close)

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
        requested_aux = msg.get("aux_tracks", "external")
        if requested_aux not in ("external", "muxed"):
            await self.send("error", code="bad_message", message="invalid aux_tracks", fatal=False)
            return
        if source_kind != "server_file" and requested_aux == "muxed":
            await self.send(
                "error", code="bad_message",
                message="muxed auxiliary tracks require server_file", fatal=False,
            )
            return
        requested_attachments = msg.get("aux_attachments", "embedded")
        if requested_attachments not in ("embedded", "cached"):
            await self.send(
                "error", code="bad_message",
                message="invalid aux_attachments", fatal=False,
            )
            return
        if requested_attachments == "cached" and requested_aux != "muxed":
            await self.send(
                "error", code="bad_message",
                message="cached attachments require muxed auxiliary tracks", fatal=False,
            )
            return
        resolved_path: str | None = None
        if source_kind == "server_file":
            if self.library is None:
                await self.send("error", code="bad_message", message="server has no library", fatal=False)
                return
            relative = source.get("path") if isinstance(source, dict) else None
            try:
                resolved = await asyncio.to_thread(self.library.resolve_file, relative or "")
                resolved_path = str(resolved)
                self.source_track = await asyncio.to_thread(VideoTrack, resolved_path)
                if requested_aux == "muxed":
                    try:
                        self.aux_track = await asyncio.to_thread(AuxiliaryTrack, resolved_path)
                    except Exception as err:
                        log.warning(
                            "session %s: cannot mux auxiliary tracks for %s; "
                            "falling back to external media: %s",
                            self.id, relative, err,
                        )
                    else:
                        attachment_bytes = self.aux_track.attachment_bytes
                        if (
                            requested_attachments == "cached"
                            and self.aux_track.attachments
                            and self.aux_track.attachment_cache_supported
                        ):
                            self.aux_track_mode = "muxed"
                            self.aux_attachment_mode = "cached"
                            self.attachment_manifest = self.aux_track.attachment_manifest()
                            self.attachment_token = new_token()
                        elif attachment_bytes > MAX_MUXED_ATTACHMENT_BYTES:
                            log.info(
                                "session %s: %.1f MiB of attachments exceeds the "
                                "live mux limit; using external auxiliary media",
                                self.id, attachment_bytes / (1024 * 1024),
                            )
                            await asyncio.to_thread(self.aux_track.close)
                            self.aux_track = None
                        else:
                            self.aux_track_mode = "muxed"
            except Exception as err:
                if self.aux_track is not None:
                    await asyncio.to_thread(self.aux_track.close)
                    self.aux_track = None
                if self.source_track is not None:
                    await asyncio.to_thread(self.source_track.close)
                    self.source_track = None
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
        # Pipeline construction can block for minutes when a model's TensorRT
        # engine is built for the first time; keepalives stop the client's
        # open_session timeout from firing meanwhile.
        build_done = asyncio.Event()
        keepalive = asyncio.create_task(self._progress_keepalive(
            "pipeline_init",
            f"Preparing {model_name} — the first use of a model builds a "
            "TensorRT engine and can take several minutes",
            build_done,
        ))
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
                seek_discard_max_s=self.seek_discard_max_s,
                aux_source_path=(resolved_path if self.aux_track_mode == "muxed" else None),
                embed_aux_attachments=self.aux_attachment_mode == "embedded",
            )
        except Exception as err:
            if isinstance(err, PipelineConstructionError):
                self.pipeline = err.pipeline
            await self.send("error", code="pipeline_error", message=str(err), fatal=True)
            # Do not await close from the open task: close must wait for this
            # task before acknowledging teardown, and the two would deadlock.
            asyncio.create_task(self.close())
            return
        finally:
            build_done.set()
            await keepalive
        if self.state == State.CLOSED:
            return
        duration_s = (self.source_track.duration_seconds() if self.source_track else
                      (msg.get("file") or {}).get("duration_s"))
        avg_rate = self.source_track.average_rate if self.source_track else cfg.avg_rate
        # server_file: chapters come from the file itself. uplink: the client
        # is the only party with the container, so echo its file.chapters —
        # session_opened.chapters is the one place clients read them from.
        chapters = (self.source_track.chapters() if self.source_track else
                    _sanitize_chapters((msg.get("file") or {}).get("chapters")))
        source_metadata = (
            {
                "source_has_audio": self.source_track.has_audio_tracks,
                "source_has_auxiliary": self.source_track.has_auxiliary_tracks,
            }
            if self.source_track is not None else {}
        )
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
            chapters=chapters or None,
            aux_tracks=self.aux_track_mode,
            aux_attachments=self.aux_attachment_mode,
            attachment_manifest=(
                self.attachment_manifest if self.aux_attachment_mode == "cached" else None
            ),
            attachment_token=(
                self.attachment_token if self.aux_attachment_mode == "cached" else None
            ),
            **source_metadata,
        )

    media_port: int = 0  # set by server at construction

    async def handle_seek(self, msg: dict) -> None:
        new_epoch = int(msg["epoch"])
        if new_epoch <= self.epoch or self.pipeline is None:
            return  # stale or premature
        self.epoch = new_epoch
        await self._stop_seek_progress()
        # Drop everything queued for the downlink writer.
        try:
            while True:
                self.down_q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self.pipeline.note_buffer_report(0)
        await self._stop_server_source()
        trace = await asyncio.to_thread(
            self.pipeline.flush, new_epoch, int(msg["target_pts"]))
        if self.downlink_attached:
            await self.start_server_source(int(msg["target_pts"]), discontinuity=True)
        # seek_ready is only an ack that the epoch switched — the first media
        # for it may be seconds away (docs/SEEK_LATENCY_PLAN.md). The ticker is
        # what tells the client the server is working rather than wedged.
        await self.send("seek_ready", epoch=new_epoch)
        self._seek_progress_task = asyncio.create_task(
            self._seek_progress_loop(trace, new_epoch))

    async def _stop_seek_progress(self) -> None:
        task, self._seek_progress_task = self._seek_progress_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _seek_progress_loop(self, trace, epoch: int) -> None:
        """Narrate a slow seek until its first downlink bytes are queued."""
        await asyncio.sleep(SEEK_PROGRESS_INITIAL_DELAY_S)
        last_progress_at = trace.requested_at
        last_indexed_s = None
        while (trace.first_packet_ms is None and epoch == self.epoch
               and self.state != State.CLOSED):
            now = time.perf_counter()
            elapsed = now - trace.requested_at
            index_progress = getattr(self.aux_track, "subtitle_index_progress", None)
            if index_progress is not None and (
                last_indexed_s is None or index_progress[1] > last_indexed_s
            ):
                last_indexed_s = index_progress[1]
                last_progress_at = now
            idle_s = now - last_progress_at
            if idle_s > SEEK_PROGRESS_MAX_S:
                log.warning(
                    "session %s: seek to %d produced no downlink bytes or "
                    "subtitle indexing progress for %.0fs (discarded %d frames) "
                    "— giving up on progress ticks",
                    self.id, trace.target_pts, idle_s, trace.frames_discarded,
                )
                return
            await self.send(
                "seek_progress",
                stage="subtitle_index" if index_progress is not None else "video_decode",
                message=("Indexing subtitles for this seek" if index_progress is not None else None),
                subtitle_indexed_s=(index_progress[1] if index_progress is not None else None),
                epoch=epoch,
                target_pts=trace.target_pts,
                keyframe_pts=trace.keyframe_pts,
                frames_discarded=trace.frames_discarded,
                elapsed_s=round(elapsed, 2),
            )
            await asyncio.sleep(SEEK_PROGRESS_INTERVAL_S)

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
        video_iterator = self.source_track.packets(from_pts)
        target_s = (
            float(from_pts * self.source_track.time_base) if from_pts is not None else None
        )
        first = True

        def merged_packets():
            """Merge independently seekable demuxers in source timestamp order."""
            sentinel = object()
            video = next(video_iterator, sentinel)
            aux_target_s = target_s
            # The optional low-latency seek mode deliberately emits video from
            # a distant preceding keyframe. Match auxiliary tracks to that
            # effective start instead of leaving seconds of silent video.
            if (
                video is not sentinel
                and target_s is not None
                and self.pipeline.seek_discard_max_s is not None
                and video.pts != NO_TS
            ):
                video_start_s = float(video.pts * self.source_track.time_base)
                if target_s - video_start_s > self.pipeline.seek_discard_max_s:
                    aux_target_s = video_start_s
            aux_iterator = (
                self.aux_track.packets(aux_target_s)
                if self.aux_track is not None else iter(())
            )
            auxiliary = next(aux_iterator, sentinel)
            while video is not sentinel or auxiliary is not sentinel:
                if video is sentinel:
                    yield "aux", auxiliary
                    auxiliary = next(aux_iterator, sentinel)
                    continue
                if auxiliary is sentinel:
                    yield "video", video
                    video = next(video_iterator, sentinel)
                    continue
                video_stamp = video.dts if video.dts != NO_TS else video.pts
                video_s = (
                    float(video_stamp * self.source_track.time_base)
                    if video_stamp != NO_TS else float("inf")
                )
                if video_s <= auxiliary.order_s:
                    yield "video", video
                    video = next(video_iterator, sentinel)
                else:
                    yield "aux", auxiliary
                    auxiliary = next(aux_iterator, sentinel)

        iterator = merged_packets()

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
                for kind, info in batch:
                    if kind == "video":
                        pkt = self.source_track.media_packet(
                            info, epoch, discontinuity=discontinuity and first
                        )
                        first = False
                        await asyncio.to_thread(self.pipeline.feed, pkt)
                    else:
                        await asyncio.to_thread(self.pipeline.feed_aux, info, epoch)
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
        """Idempotent resource-release barrier for the control connection."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_impl(asyncio.current_task()))
        await asyncio.shield(self._close_task)

    async def _close_impl(self, initiator: asyncio.Task | None = None) -> None:
        errors: list[BaseException] = []
        if self.state != State.CLOSED:
            await self.set_state(State.CLOSED)
        self.down_q.retire()
        await self._close_media(initiator)
        await self._stop_seek_progress()
        await self._stop_server_source()

        # A cancelled asyncio.to_thread build keeps running. Wait for the open
        # task and its _open_and_reap finally block so it cannot publish a late
        # pipeline after this barrier has opened.
        open_task = self._open_task
        if open_task is not None and open_task is not asyncio.current_task():
            result = await asyncio.gather(open_task, return_exceptions=True)
            if result and isinstance(result[0], BaseException):
                errors.append(result[0])
        self._open_task = None

        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            if hasattr(pipeline, "stats") and not getattr(pipeline, "construction_failed", False):
                self._final_pipeline_status = self._pipeline_status(pipeline)
            try:
                await asyncio.to_thread(pipeline.close)
            except BaseException as err:
                errors.append(err)
            finally:
                # Keep the completed run's diagnostics available to callers
                # holding a Session reference after the teardown barrier.
                if hasattr(pipeline, "stats") and not getattr(pipeline, "construction_failed", False):
                    final_status = self._pipeline_status(pipeline)
                    if self._final_pipeline_status is not None:
                        final_status["provider"] = (
                            final_status["provider"]
                            or self._final_pipeline_status["provider"]
                        )
                    self._final_pipeline_status = final_status

        if self.source_track is not None:
            source, self.source_track = self.source_track, None
            try:
                await asyncio.to_thread(source.close)
            except BaseException as err:
                errors.append(err)
        if self.aux_track is not None:
            auxiliary, self.aux_track = self.aux_track, None
            try:
                await asyncio.to_thread(auxiliary.close)
            except BaseException as err:
                errors.append(err)

        # Release queued payloads even if the peer vanished before consuming
        # them. Wake any internal consumer which was not a media attachment.
        while not self.down_q.empty():
            self.down_q.get_nowait()
        self.down_q.put_nowait(None)
        if errors:
            raise errors[0]

    def _pipeline_status(self, p: Pipeline) -> dict:
        return {
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
            "provider": getattr(p.upscaler, "active_provider", None),
            "last_seek": p.stats.last_seek.report() if p.stats.last_seek else None,
        }

    def status(self) -> dict:
        p = self.pipeline
        if p is not None and getattr(p, "construction_failed", False):
            p = None
        return {
            "id": self.id,
            "state": self.state.value,
            "epoch": self.epoch,
            "uplink_attached": self.uplink_attached,
            "downlink_attached": self.downlink_attached,
            "source": self.source_kind,
            "aux_tracks": self.aux_track_mode,
            "aux_attachments": self.aux_attachment_mode,
            "pipeline": self._pipeline_status(p) if p is not None else self._final_pipeline_status,
        }
