"""Async relay client: control channel + uplink/downlink media streams.

Usage sketch (see cli.py for a complete example):

    client = RelayClient("127.0.0.1", 8590)
    await client.connect()
    session = await client.open_session(SessionConfig(path="movie.mkv", ...))
    await client.attach_media()
    client.start_uplink()
    await client.play()
    async for pkt in client.downlink():   # decoded elsewhere
        ...
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

import aiohttp

from relay_protocol import (
    DIR_DOWNLINK,
    DIR_UPLINK,
    FLAG_EOS,
    PROTOCOL_VERSION,
    MediaPacket,
    build_handshake,
    encode_packet,
    read_packet_sync,
)

from .demux import VideoTrack
from .attachments import materialize_attachment_cache, remove_attachment_view

log = logging.getLogger("relay.client")


def _open_local_source(path: str):
    """Open and inspect a potentially remote mounted file on one worker."""
    track = VideoTrack(path)
    try:
        return track, track.open_session_video_dict(), track.duration_seconds(), track.chapters()
    except BaseException:
        track.close()
        raise


class _SourceOpening:
    """Keep native ownership on the worker until the loop accepts the source."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._abandoned = False
        self._result = None

    def run(self) -> None:
        opened = _open_local_source(self.path)
        with self._lock:
            if not self._abandoned:
                self._result = opened
                return
        # Cancellation can return to the GUI immediately; the still-running
        # native opener releases its own eventual result on this worker.
        opened[0].close()

    def take(self):
        with self._lock:
            result, self._result = self._result, None
        return result

    def abandon(self):
        with self._lock:
            self._abandoned = True
            result, self._result = self._result, None
        return result[0] if result is not None else None


def _observe_source_cleanup(future: asyncio.Future) -> None:
    if not future.cancelled() and (error := future.exception()) is not None:
        log.warning("abandoned source cleanup failed: %r", error)

# Media pumping is sized for a slow event loop, not a fast one: under qasync
# the loop shares the GUI thread with mpv rendering and turns come roughly
# once per painted frame (~25/s at 24 fps). Anything that needs a loop turn
# per packet caps out near half the frame rate — observed as the uplink
# starving the server below realtime while playback was smooth.
_UPLINK_BATCH = 16  # packets demuxed+sent per loop-turn pair
_DOWNLINK_BATCH = 8  # one Qt-loop wakeup per batch, not per lossless frame
_DOWNLINK_BATCH_BYTES = 16 * 1024 * 1024
_DOWNLINK_SOCKET_BUFFER = 4 * 1024 * 1024

# open_session inactivity window: each server session_progress keepalive
# (docs/PROTOCOL.md — ticked during first-use TensorRT engine builds, which
# run for minutes) refreshes the deadline, so a building server never times
# out while a silent one still fails within this window. Against a server
# without keepalives this degrades to the old fixed 240 s timeout.
OPEN_SESSION_TIMEOUT_S = 240.0
TEARDOWN_TIMEOUT_S = 30.0
# After a teardown the server never acknowledged, its /status says when the
# session is gone (RelayClient.wait_session_released). The server's slowest
# allowed release is ~30 s to notice a dead control connection (20 s heartbeat
# plus the pong timeout) and then a 15 s pipeline close, with the session
# listed throughout: 45 s. The bound is that with a margin, so a client which
# reconnects at once does not give up just as the release is being reported.
SESSION_RELEASE_TIMEOUT_S = 60.0
SESSION_RELEASE_POLL_S = 1.0
STATUS_REQUEST_TIMEOUT_S = 10.0
# Client-side ping on the control WebSocket. The server pings too, but a peer
# that vanished without a FIN (laptop suspend, Wi-Fi drop) leaves this end
# looking connected until the next request; with a heartbeat aiohttp closes
# the socket after a missed pong and the control reader ends.
CONTROL_HEARTBEAT_S = 20.0


class TeardownNotConfirmedError(RuntimeError):
    """The client closed locally without the server's native-release barrier.

    ``session_id`` names the server session whose release went unconfirmed,
    when a later connection can still settle it through
    ``RelayClient.wait_session_released``. It is None when nothing the server
    lists can: the session id never arrived, the server said it needs a
    restart, or a local owner failed to close.
    """

    def __init__(self, message: str, *, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


def _unreadable_status(session_id: str, detail: str) -> TeardownNotConfirmedError:
    return TeardownNotConfirmedError(
        f"the server's /status could not say whether session {session_id} was "
        f"released ({detail}); do not open a replacement session until the "
        "server is checked",
        session_id=session_id,
    )


def _take_downlink_batch(
    batch: list[MediaPacket | None], pkt: MediaPacket, current_epoch: int,
) -> list[MediaPacket | None] | None:
    """Route one packet, publishing epoch boundaries without qasync delay."""
    if batch and batch[0] is not None and batch[0].epoch < current_epoch:
        batch.clear()
    if pkt.epoch < current_epoch:
        return None
    if pkt.discontinuity:
        # A new epoch must never sit behind a stale partial batch. Publishing
        # this one packet costs exactly one extra event-loop wakeup per epoch.
        batch.clear()
        return [pkt]
    batch.append(pkt)
    if (len(batch) >= _DOWNLINK_BATCH or pkt.eos
            or sum(len(item.payload) for item in batch if item is not None) >= _DOWNLINK_BATCH_BYTES):
        ready = list(batch)
        batch.clear()
        return ready
    return None


class _ThreadBridgeQueue:
    """A bounded queue written by a socket thread and awaited by asyncio.

    The producer schedules one event-loop wakeup per batch. Queue storage and
    backpressure live behind a threading.Condition, so the media reader never
    calls asyncio.Queue methods from outside the event-loop thread.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int,
                 max_bytes: int = 128 * 1024 * 1024):
        if maxsize < 1 or max_bytes < 1:
            raise ValueError("queue limits must be positive")
        self._loop = loop
        self._maxsize = maxsize
        self._max_bytes = max_bytes
        self._queued_bytes = 0
        self._items = collections.deque()
        self._condition = threading.Condition()
        self._available = asyncio.Event()
        self._closed = False

    def put_batch_from_thread(self, items: list[MediaPacket | None]) -> bool:
        if any(item is not None and len(item.payload) > self._max_bytes for item in items):
            raise ValueError("packet exceeds downlink queue byte budget")
        index = 0
        while index < len(items):
            with self._condition:
                item = items[index]
                size = len(item.payload) if item is not None else 0
                while not self._closed and (
                    len(self._items) >= self._maxsize
                    or self._queued_bytes + size > self._max_bytes
                ):
                    self._condition.wait(timeout=0.25)
                if self._closed:
                    return False
                # Publish the largest prefix that fits, waking the consumer
                # before waiting for space for the remainder of a large batch.
                while index < len(items):
                    item = items[index]
                    size = len(item.payload) if item is not None else 0
                    if (len(self._items) >= self._maxsize
                            or self._queued_bytes + size > self._max_bytes):
                        break
                    self._items.append(item)
                    self._queued_bytes += size
                    index += 1
            self._wake()
        return True

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._available.set)
        except RuntimeError:
            pass  # event loop already closed during interpreter/app shutdown

    async def get(self) -> MediaPacket | None:
        while True:
            with self._condition:
                if self._items:
                    item = self._items.popleft()
                    self._queued_bytes -= len(item.payload) if item is not None else 0
                    if not self._items:
                        self._available.clear()
                    self._condition.notify_all()
                    return item
                if self._closed:
                    return None
                # Clear while holding the same lock used by the producer. If
                # it appends immediately afterward, its scheduled set() cannot
                # be lost between this check and the await.
                self._available.clear()
            await self._available.wait()

    def get_nowait(self) -> MediaPacket | None:
        with self._condition:
            if not self._items:
                raise asyncio.QueueEmpty
            item = self._items.popleft()
            self._queued_bytes -= len(item.payload) if item is not None else 0
            if not self._items:
                self._available.clear()
            self._condition.notify_all()
            return item

    def qsize(self) -> int:
        with self._condition:
            return len(self._items)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._items.clear()
            self._queued_bytes = 0
            self._condition.notify_all()
        self._wake()


@dataclass
class SessionConfig:
    path: str
    model: str = "passthrough"
    quality_tier: str = "lossless-hevc"
    display_w: int = 1920
    display_h: int = 1080
    # "fit": preserve the whole frame. "cover": server-side center crop to
    # the display aspect ratio. See docs/PROTOCOL.md open_session.
    fit_mode: str = "fit"
    # "uplink" reads ``path`` on the client; "server_file" treats it as a
    # share-relative path inside the server's configured library. Kept in its
    # original positional slot so existing SessionConfig calls retain meaning.
    source: str = "uplink"
    # None asks the server to use its configured default.
    resize_algorithm: str | None = None
    # Server-library sessions can ask a capable server to stream-copy original
    # audio/subtitle tracks into each epoch's Matroska downlink. "external"
    # retains the /media attachment path used by older clients and servers.
    aux_tracks: str = "external"
    # "cached" asks a capable server to omit immutable font bodies from each
    # epoch and expose a verified content-addressed manifest instead.
    aux_attachments: str = "embedded"


@dataclass
class SessionInfo:
    session_id: str
    media_port: int
    uplink_token: str | None
    downlink_token: str
    downlink_codec: str
    downlink_extradata: bytes | None
    downlink_width: int
    downlink_height: int
    downlink_container: str | None = None  # "matroska": payload is container bytes
    source: str = "uplink"
    time_base: Fraction | None = None
    duration_s: float | None = None
    avg_rate: Fraction | None = None
    fit_mode: str = "fit"
    resize_algorithm: str | None = None
    # Wire-format chapter dicts ({start_s, end_s?, title?}), sorted by start_s;
    # None when the source has none (docs/PROTOCOL.md session_opened).
    chapters: list[dict] | None = None
    aux_tracks: str = "external"
    aux_attachments: str = "embedded"
    attachment_manifest: list[dict] | None = None
    attachment_token: str | None = None
    # Server-file metadata; None preserves attachment behavior with old peers.
    source_has_audio: bool | None = None
    source_has_auxiliary: bool | None = None


class RelayClient:
    def __init__(self, host: str, port: int):
        self._loop = asyncio.get_running_loop()
        self.host = host
        self.port = port
        self._http = aiohttp.ClientSession()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.capabilities: dict | None = None
        self.session: SessionInfo | None = None
        self._has_server_session = False
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self.track: VideoTrack | None = None
        self.epoch = 0
        self.state = "idle"
        self._pending: dict[str | tuple[str, int], asyncio.Future] = {}
        self._down_q = _ThreadBridgeQueue(self._loop, maxsize=1024)
        self._uplink_writer: asyncio.StreamWriter | None = None
        self._uplink_task: asyncio.Task | None = None
        # Counts the demux iterators start_uplink has claimed: the newest one
        # owns the source (see _uplink_loop).
        self._uplink_generation = 0
        self._reader_task: asyncio.Task | None = None
        self._downlink_socket: socket.socket | None = None
        self._downlink_thread: threading.Thread | None = None
        self._downlink_stop = threading.Event()
        self._downlink_ready: asyncio.Future | None = None
        self._downlink_stats_lock = threading.Lock()
        self._downlink_bytes_total = 0
        self._downlink_packets_total = 0
        self._downlink_sample_bytes = 0
        self._downlink_sample_at = time.monotonic()
        self.errors: list[dict] = []
        # Set, with the downlink queue ended, when reading the local source
        # failed: the message the consumer should show for the ended stream.
        self.source_error: str | None = None
        self.buffered_ms = 0  # consumer updates; buffer_report loop sends it
        # Optional UI hooks: called with each session_progress / seek_progress
        # message dict (from the control-reader task, i.e. on the event-loop
        # thread).
        self.on_progress = None
        self.on_seek_progress = None
        # Called with no arguments (same task/thread) when the control
        # connection ends without close()/teardown() having been requested.
        self.on_disconnected = None
        self._last_activity = time.monotonic()
        self._attachment_view_dir: Path | None = None

    # -- control channel -------------------------------------------------------

    @property
    def has_server_session(self) -> bool:
        """Also true during an open whose server allocation is not yet known."""
        return self._has_server_session

    @property
    def connected(self) -> bool:
        """False once the control connection has ended, for any reason."""
        return (
            self._ws is not None and not self._ws.closed
            and self._reader_task is not None and not self._reader_task.done()
        )

    @property
    def base_url(self) -> str:
        host = self.host.strip("[]")
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    async def connect(self) -> dict:
        self._ws = await self._http.ws_connect(
            f"{self.base_url}/control", heartbeat=CONTROL_HEARTBEAT_S,
        )
        self._reader_task = asyncio.create_task(self._control_reader())
        self.capabilities = await self._request(
            "capabilities", "hello",
            protocol_version=PROTOCOL_VERSION, client_name="relay-client-core",
            display={"w": 0, "h": 0},
        )
        return self.capabilities

    async def _send(self, type_: str, **fields) -> None:
        assert self._ws is not None
        await self._ws.send_str(json.dumps({"type": type_, **fields}))

    async def _request(self, expect: str, type_: str, timeout: float = 30,
                       keepalive: bool = False, **fields) -> dict:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        key = (expect, fields["epoch"]) if expect == "seek_ready" else expect
        if key in self._pending:
            raise RuntimeError(f"a {expect} request is already pending")
        self._pending[key] = fut
        self._last_activity = time.monotonic()
        try:
            await self._send(type_, **fields)
            if not keepalive:
                return await asyncio.wait_for(fut, timeout=timeout)
            # Inactivity deadline: progress keepalives push it out, so a
            # building server remains alive while a silent one still fails.
            while True:
                remaining = self._last_activity + timeout - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"{type_}: no reply or progress for {timeout:.0f}s")
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(fut), timeout=min(remaining, 2.0))
                except asyncio.TimeoutError:
                    continue
        except BaseException:
            if self._pending.get(key) is fut:
                del self._pending[key]
            fut.cancel()
            raise

    async def _control_reader(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = json.loads(raw.data)
                mtype = msg.get("type")
                if mtype == "session_progress":
                    self._last_activity = time.monotonic()
                    if self.on_progress is not None:
                        try:
                            self.on_progress(msg)
                        except Exception:
                            log.exception("on_progress callback failed")
                    continue
                if mtype == "seek_progress":
                    # Purely informational: seek_ready already acked the epoch, so
                    # this never resolves a pending request.
                    if self.on_seek_progress is not None:
                        try:
                            self.on_seek_progress(msg)
                        except Exception:
                            log.exception("on_seek_progress callback failed")
                    continue
                if mtype == "state":
                    self.state = msg["state"]
                elif mtype == "error":
                    log.warning("server error: %s", msg)
                    self.errors.append(msg)
                    # Requests are sequential; an error while any request is
                    # pending is that request's answer — fail it immediately.
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.set_exception(
                                RuntimeError(f"{msg.get('code')}: {msg.get('message', '')}")
                            )
                    self._pending.clear()
                key = (mtype, msg.get("epoch")) if mtype == "seek_ready" else mtype
                fut = self._pending.pop(key, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        finally:
            # Wake a teardown/open/seek request immediately when the control
            # connection disappears instead of stranding it until timeout.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("control connection closed"))
            self._pending.clear()
            if not self._closing and self.on_disconnected is not None:
                try:
                    self.on_disconnected()
                except Exception:
                    log.exception("on_disconnected callback failed")

    async def open_session(self, cfg: SessionConfig) -> SessionInfo:
        if cfg.source not in ("uplink", "server_file"):
            raise ValueError(f"unknown session source: {cfg.source}")
        if cfg.aux_tracks not in ("external", "muxed"):
            raise ValueError(f"unknown auxiliary-track mode: {cfg.aux_tracks}")
        if cfg.source != "server_file" and cfg.aux_tracks != "external":
            raise ValueError("muxed auxiliary tracks require a server_file session")
        if cfg.aux_attachments not in ("embedded", "cached"):
            raise ValueError(f"unknown auxiliary attachment mode: {cfg.aux_attachments}")
        if cfg.aux_attachments == "cached" and cfg.aux_tracks != "muxed":
            raise ValueError("cached attachments require muxed auxiliary tracks")
        if self._closing:
            raise ConnectionError("client is closing")
        video, duration_s, chapters = None, None, []
        if cfg.source == "uplink":
            owner = _SourceOpening(cfg.path)
            # Use an executor Future, not another asyncio Task: cancellation
            # of all loop tasks must not discard a native worker's result.
            opening = self._loop.run_in_executor(None, owner.run)
            try:
                await asyncio.shield(opening)
            except asyncio.CancelledError:
                track = owner.abandon()
                opening.add_done_callback(_observe_source_cleanup)
                if track is not None:
                    cleanup = self._loop.run_in_executor(None, track.close)
                    cleanup.add_done_callback(_observe_source_cleanup)
                raise
            if self._closing:
                track = owner.abandon()
                if track is not None:
                    cleanup = self._loop.run_in_executor(None, track.close)
                    cleanup.add_done_callback(_observe_source_cleanup)
                raise ConnectionError("client closed while opening source")
            track, video, duration_s, chapters = owner.take()
            self.track = track
        source = ("uplink" if cfg.source == "uplink" else
                  {"type": "server_file", "path": cfg.path})
        # keepalive=True: a cold TensorRT engine build at session open can run
        # for minutes; the server's session_progress ticks keep the request
        # alive (OPEN_SESSION_TIMEOUT_S is a window of *inactivity*, not a cap
        # on total build time).
        file_info = {"name": cfg.path, "duration_s": duration_s}
        if chapters:
            file_info["chapters"] = chapters
        fields = {
            "source": source,
            "file": file_info,
            "model": cfg.model,
            "quality_tier": cfg.quality_tier,
            "display": {"w": cfg.display_w, "h": cfg.display_h},
            "fit_mode": cfg.fit_mode,
            "aux_tracks": cfg.aux_tracks,
            "aux_attachments": cfg.aux_attachments,
        }
        if cfg.resize_algorithm is not None:
            fields["resize_algorithm"] = cfg.resize_algorithm
        if video is not None:
            fields["video"] = video
        try:
            self._has_server_session = True
            msg = await self._request(
                "session_opened", "open_session",
                timeout=OPEN_SESSION_TIMEOUT_S, keepalive=True, **fields,
            )
        except BaseException:
            if self.track is not None:
                track = self.track
                self.track = None
                await asyncio.to_thread(track.close)
            raise
        self.session = SessionInfo(
            session_id=msg["session_id"],
            media_port=msg["media_port"],
            uplink_token=msg["uplink_token"],
            downlink_token=msg["downlink_token"],
            downlink_codec=msg["downlink_codec"],
            downlink_extradata=base64.b64decode(msg["downlink_extradata_b64"])
            if msg.get("downlink_extradata_b64")
            else None,
            downlink_width=msg["downlink_width"],
            downlink_height=msg["downlink_height"],
            downlink_container=msg.get("downlink_container"),
            source=msg.get("source", cfg.source),
            time_base=Fraction(*msg["time_base"]) if msg.get("time_base") else None,
            duration_s=msg.get("duration_s"),
            avg_rate=Fraction(*msg["avg_rate"]) if msg.get("avg_rate") else None,
            fit_mode=msg.get("fit_mode", cfg.fit_mode),
            resize_algorithm=msg.get("resize_algorithm", cfg.resize_algorithm),
            chapters=msg.get("chapters") or None,
            aux_tracks=msg.get("aux_tracks", "external"),
            aux_attachments=msg.get("aux_attachments", "embedded"),
            attachment_manifest=msg.get("attachment_manifest") or None,
            attachment_token=msg.get("attachment_token"),
            source_has_audio=(
                msg["source_has_audio"] if type(msg.get("source_has_audio")) is bool else None
            ),
            source_has_auxiliary=(
                msg["source_has_auxiliary"]
                if type(msg.get("source_has_auxiliary")) is bool else None
            ),
        )
        return self.session

    async def prepare_attachments(self, cache_root: Path) -> Path | None:
        """Materialize negotiated cached fonts before mpv loads the epoch."""
        if self._closing:
            raise ConnectionError("client is closing")
        session = self.session
        if session is None or session.aux_attachments != "cached":
            return None
        token = session.attachment_token
        if not token:
            raise RuntimeError("cached attachment session omitted its token")
        previous_view = self._attachment_view_dir
        self._attachment_view_dir = None
        await remove_attachment_view(previous_view)
        if self._closing or self.session is not session:
            raise ConnectionError("client closed while preparing attachments")
        view = await materialize_attachment_cache(
            self._http,
            self.base_url,
            session.session_id,
            session.attachment_manifest or [],
            token,
            Path(cache_root),
        )
        if self._closing or self.session is not session:
            # close() may have finished while a disk worker created this view.
            # It never owned that unpublished lease, so the opening operation
            # must release it rather than assigning it to an already closed client.
            await remove_attachment_view(view)
            raise ConnectionError("client closed while preparing attachments")
        self._attachment_view_dir = view
        return self._attachment_view_dir

    async def fetch_library_page(
        self, path: str = "", *, cursor: str | None = None, limit: int = 100,
        sort: str | None = None,
    ) -> dict:
        """Fetch one page of a directory's immediate children."""
        params = {"path": path, "limit": str(limit)}
        if sort is not None:
            params["sort"] = sort
        if cursor is not None:
            params["cursor"] = cursor
        async with self._http.get(
            f"{self.base_url}/library", params=params,
        ) as response:
            response.raise_for_status()
            payload = await response.json()
            return {
                "tree": payload["tree"],
                "next_cursor": payload.get("next_cursor"),
            }

    def media_url(self, relative_path: str) -> str:
        path = quote(relative_path, safe="/")
        return f"{self.base_url}/media/{path}"

    # -- media ------------------------------------------------------------------

    async def attach_media(self) -> None:
        assert self.session is not None
        if self.session.uplink_token is not None:
            up_r, up_w = await asyncio.open_connection(self.host, self.session.media_port)
            up_w.write(build_handshake(DIR_UPLINK, self.session.uplink_token))
            await up_w.drain()
            if await up_r.readexactly(1) != b"\x00":
                raise RuntimeError("uplink handshake rejected")
            self._uplink_writer = up_w
        self._downlink_ready = self._loop.create_future()
        self._downlink_thread = threading.Thread(
            target=self._downlink_receiver_work,
            name="relay-downlink",
            daemon=True,
        )
        self._downlink_thread.start()
        try:
            await asyncio.wait_for(asyncio.shield(self._downlink_ready), timeout=30)
        except BaseException:
            if not self._downlink_ready.done():
                self._downlink_ready.cancel()
            self._stop_downlink_receiver()
            if self._downlink_thread.is_alive():
                await asyncio.to_thread(self._downlink_thread.join, 5.0)
            raise
        self._report_task = asyncio.create_task(self._buffer_report_loop())

    async def start_uplink(self, from_pts: int | None = None, discontinuity: bool = False,
                           epoch: int | None = None) -> None:
        if self.track is None:
            return
        if epoch is None:
            epoch = self.epoch
        # The demuxer is single-threaded state: the old task must be fully done
        # before we seek it and start a new epoch's iteration.
        if self._uplink_task is not None:
            self._uplink_task.cancel()
            try:
                await self._uplink_task
            except (asyncio.CancelledError, Exception):
                pass
        # Bind the epoch NOW, not when the task first runs: a rapid follow-up
        # seek can bump self.epoch before the task is scheduled, and a stale
        # task stamping the new epoch interleaves two streams of one epoch
        # (docs/PROTOCOL.md §4 forbids exactly this).
        if epoch != self.epoch or self.track is None or self._closing:
            return
        # Claim the demux iterator here, on the event loop, never on a worker.
        # packets() only takes a new iterator generation (the seek and every
        # read run later, on the pump's worker), but a cancelled to_thread
        # keeps running: an obsolete pump whose worker claimed its iterator
        # after this one superseded it, and this epoch's stream ended early.
        self._uplink_generation += 1
        iterator = self.track.packets(from_pts)
        self._uplink_task = asyncio.create_task(
            self._uplink_loop(iterator, self._uplink_generation, discontinuity, epoch))

    async def _uplink_loop(self, iterator, generation: int, discontinuity: bool,
                           epoch: int) -> None:
        assert self.track is not None and self._uplink_writer is not None
        first = True

        def next_batch() -> list:
            batch = []
            for info in iterator:
                batch.append(info)
                if len(batch) >= _UPLINK_BATCH:
                    break
            return batch

        def current() -> bool:
            # VideoTrack ends a superseded iterator quietly, just as it ends at
            # the end of the file. Only the newest pump of the current epoch
            # may take a short batch for the end of the source.
            return (epoch == self.epoch and generation == self._uplink_generation
                    and not self._closing)

        async def send(data: bytes) -> bool:
            try:
                self._uplink_writer.write(data)
                await self._uplink_writer.drain()
            except OSError as err:
                # The server closed the uplink (its session is ending, which
                # the control channel reports) or the connection dropped: not
                # a source failure, whatever errno the socket gave.
                log.info("uplink closed: %r", err)
                return False
            return True

        try:
            while True:
                # One thread hop + one drain per batch: per-packet round-trips
                # need ~2 loop turns each and starve the server when the GUI
                # loop is slow (see _UPLINK_BATCH above).
                batch = await asyncio.to_thread(next_batch)
                if not current():
                    return
                buf = bytearray()
                for info in batch:
                    pkt = self.track.media_packet(info, epoch,
                                                  discontinuity=discontinuity and first)
                    first = False
                    buf += encode_packet(pkt)
                if buf and not await send(bytes(buf)):
                    return
                if len(batch) < _UPLINK_BATCH:  # iterator exhausted
                    break
            if current():
                await send(encode_packet(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=epoch)))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            # Reading the source failed (an SMB read, a damaged file). PyAV
            # raises OSError subclasses, ConnectionResetError among them, so
            # only the call site tells this apart from a closed uplink. A pump
            # a seek already replaced stays quiet: its successor reads anew.
            if current():
                self._fail_source(err)

    def _fail_source(self, err: Exception) -> None:
        """End this session's media after reading the local source failed.

        The server would otherwise wait for packets indefinitely. Record why
        and end the downlink queue (get() returns None, without EOS): its
        consumer stops playback and the session's owner tears down, as for
        any ended downlink.
        """
        log.warning("source read failed, ending playback: %r", err, exc_info=err)
        self.source_error = f"Could not read the source file: {str(err) or type(err).__name__}"
        self._down_q.close()

    def _finish_downlink_setup(self, error: Exception | None = None) -> None:
        future = self._downlink_ready
        if future is None or future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def _downlink_receiver_work(self) -> None:
        """Blocking high-throughput media receiver, isolated from qasync."""
        assert self.session is not None
        sock: socket.socket | None = None
        ready = False
        batch: list[MediaPacket | None] = []
        try:
            sock = socket.create_connection(
                (self.host, self.session.media_port), timeout=30
            )
            sock.settimeout(None)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _DOWNLINK_SOCKET_BUFFER)
            except OSError:
                pass  # platform buffer limits vary; blocking large reads still fix qasync pacing
            self._downlink_socket = sock
            sock.sendall(build_handshake(DIR_DOWNLINK, self.session.downlink_token))
            if sock.recv(1) != b"\x00":
                raise RuntimeError("downlink handshake rejected")
            ready = True
            try:
                self._loop.call_soon_threadsafe(self._finish_downlink_setup)
            except RuntimeError:
                return

            while not self._downlink_stop.is_set():
                pkt = read_packet_sync(sock)
                with self._downlink_stats_lock:
                    self._downlink_bytes_total += len(pkt.payload)
                    self._downlink_packets_total += 1
                ready_batch = _take_downlink_batch(batch, pkt, self.epoch)
                if ready_batch is not None:
                    if not self._down_q.put_batch_from_thread(ready_batch):
                        return
        except (EOFError, OSError, ConnectionError, RuntimeError, ValueError) as err:
            if not ready:
                try:
                    self._loop.call_soon_threadsafe(self._finish_downlink_setup, err)
                except RuntimeError:
                    pass
            elif not self._downlink_stop.is_set():
                log.info("downlink closed: %r", err)
        finally:
            if ready and batch and not self._downlink_stop.is_set():
                current = self.epoch
                batch = [p for p in batch if p is None or p.epoch >= current]
                self._down_q.put_batch_from_thread(batch)
            if ready and not self._downlink_stop.is_set():
                self._down_q.put_batch_from_thread([None])
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if self._downlink_socket is sock:
                self._downlink_socket = None

    def _stop_downlink_receiver(self) -> None:
        self._downlink_stop.set()
        self._down_q.close()
        sock = self._downlink_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    async def _buffer_report_loop(self) -> None:
        # First report goes out immediately so the server knows the buffer
        # state before the pipeline outruns it.
        while True:
            try:
                await self._send("buffer_report", buffered_ms=int(self.buffered_ms))
            except (ConnectionResetError, RuntimeError) as err:
                # Only a dead control WS lands here; the session is over. The
                # server's pacing decays a silent client's last report, so a
                # lost reporter no longer wedges it — but make the exit loud.
                log.warning("buffer report loop exiting: %r", err)
                return
            await asyncio.sleep(0.5)

    def downlink_queue(self) -> _ThreadBridgeQueue:
        return self._down_q

    def downlink_stats(self) -> dict:
        """Snapshot receive throughput and the pre-consumer bridge depth."""
        now = time.monotonic()
        with self._downlink_stats_lock:
            total_bytes = self._downlink_bytes_total
            total_packets = self._downlink_packets_total
            elapsed = max(0.001, now - self._downlink_sample_at)
            mbps = (total_bytes - self._downlink_sample_bytes) * 8 / elapsed / 1_000_000
            self._downlink_sample_bytes = total_bytes
            self._downlink_sample_at = now
        return {
            "mbps": mbps,
            "queue_packets": self._down_q.qsize(),
            "total_bytes": total_bytes,
            "total_packets": total_packets,
        }

    # -- transport-level commands --------------------------------------------------

    async def play(self) -> None:
        await self._send("play")

    async def pause(self) -> None:
        await self._send("pause")

    async def seek(self, target_pts: int) -> None:
        """Full docs/PROTOCOL.md §4 seek dance."""
        self.epoch += 1
        epoch = self.epoch
        # The server may acknowledge only the newest seek. Retire old waiters
        # immediately; an old acknowledgement must never resolve the new one.
        for key, future in list(self._pending.items()):
            if isinstance(key, tuple) and key[0] == "seek_ready":
                self._pending.pop(key)
                if not future.done():
                    future.set_result({"type": "seek_ready", "epoch": key[1]})
        if self._uplink_task is not None:
            self._uplink_task.cancel()
        # Drop already-received downlink data.
        try:
            while True:
                self._down_q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self.buffered_ms = 0
        msg = await self._request("seek_ready", "seek", target_pts=target_pts, epoch=epoch)
        if msg["epoch"] < self.epoch:
            return  # superseded by a newer seek
        if self.track is not None:
            await self.start_uplink(from_pts=target_pts, discontinuity=True, epoch=epoch)

    async def teardown(self) -> None:
        for task in (self._uplink_task, getattr(self, "_report_task", None)):
            if task is not None:
                task.cancel()
        # close() forgets the session; an unacknowledged teardown names it.
        session = getattr(self, "session", None)
        session_id = session.session_id if session is not None else None
        barrier_error: BaseException | None = None
        if self._has_server_session and self._ws is not None and not self._ws.closed:
            try:
                await self._request(
                    "closed", "teardown", timeout=TEARDOWN_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                # Cancelling the acknowledgement wait must still release local
                # sockets, demux and fonts. Start the shared cleanup owner now;
                # a slow mounted-file close need not hold the cancelled caller.
                self._begin_close()
                raise
            except (asyncio.TimeoutError, ConnectionError, ConnectionResetError,
                    RuntimeError) as err:
                barrier_error = err
        elif self._has_server_session:
            barrier_error = ConnectionError(
                "control connection was already closed before teardown"
            )
        local_close_failed = False
        try:
            await self.close()
        except BaseException:
            local_close_failed = True
            raise
        finally:
            if barrier_error is not None:
                # /status can report the server's session gone; it cannot
                # clear a server that asked for a restart, nor vouch for a
                # local owner that did not close.
                restart_required = any(
                    error.get("code") == "server_restart_required"
                    for error in getattr(self, "errors", ())
                )
                raise TeardownNotConfirmedError(
                    "server did not confirm native session resource release; "
                    "do not open a replacement session until the server is checked",
                    session_id=(
                        None if local_close_failed or restart_required else session_id
                    ),
                ) from barrier_error

    async def wait_session_released(
        self, session_id: str, *, timeout: float = SESSION_RELEASE_TIMEOUT_S,
        interval: float = SESSION_RELEASE_POLL_S,
        request_timeout: float = STATUS_REQUEST_TIMEOUT_S,
    ) -> None:
        """Return once the server's /status no longer lists ``session_id``.

        For the connection that replaces one whose teardown went
        unacknowledged (``TeardownNotConfirmedError.session_id``): nothing may
        open a session on that server until this has returned. The server
        lists a session until its native close has returned, and reports a
        close that failed as ``restart_required``; an absent id with that flag
        clear therefore means its encoder and inference owners are released.

        The first poll is immediate, later ones ``interval`` apart. Raises
        TeardownNotConfirmedError when the server says otherwise or cannot
        say: ``restart_required`` is set (checked first, whichever session
        failed), the session is still listed ``timeout`` seconds after the
        first poll, or /status is not a readable answer. Raises
        ConnectionError when the request itself failed (refused, reset, timed
        out): that is no verdict, and the question stays open for the next
        connection.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            status = await self._read_status(session_id, request_timeout)
            restart_required = status.get("restart_required", False)
            if restart_required is True:
                raise TeardownNotConfirmedError(
                    "the server reports a native session teardown that did not "
                    "complete; restart the relay server before opening another session",
                    session_id=session_id,
                )
            if restart_required is not False:
                raise _unreadable_status(session_id, "restart_required is not true or false")
            sessions = status.get("sessions")
            if not isinstance(sessions, list):
                raise _unreadable_status(session_id, "it has no session list")
            listed = False
            for entry in sessions:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    raise _unreadable_status(session_id, "it lists a session without an id")
                listed = listed or entry["id"] == session_id
            if not listed:
                return
            waited = loop.time() - started
            if waited >= timeout:
                raise TeardownNotConfirmedError(
                    f"the server still lists session {session_id} {waited:.0f} s after its "
                    "teardown went unacknowledged; do not open a replacement session "
                    "until the server is checked",
                    session_id=session_id,
                )
            await asyncio.sleep(interval)

    async def _read_status(self, session_id: str, request_timeout: float) -> dict:
        try:
            async with self._http.get(
                f"{self.base_url}/status", allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=request_timeout),
            ) as response:
                http_status = response.status
                body = await response.read()
        except aiohttp.ClientResponseError as err:
            # Something answered, and not with HTTP a relay server speaks.
            raise _unreadable_status(session_id, f"malformed response: {err.message}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            raise ConnectionError(
                f"could not ask the server whether session {session_id} was released: "
                f"{str(err) or type(err).__name__}"
            ) from err
        if not 200 <= http_status < 300:
            raise _unreadable_status(session_id, f"HTTP {http_status}")
        try:
            status = json.loads(body)
        except ValueError as err:  # not JSON, or not text at all
            raise _unreadable_status(session_id, "it is not JSON") from err
        if not isinstance(status, dict):
            raise _unreadable_status(session_id, "it is not a JSON object")
        return status

    def _begin_close(self) -> asyncio.Task:
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        return self._close_task

    async def close(self) -> None:
        await asyncio.shield(self._begin_close())

    async def _close(self) -> None:
        tasks = [t for t in (self._uplink_task, self._reader_task,
                             getattr(self, "_report_task", None)) if t is not None]
        for task in tasks:
            task.cancel()
        # The uplink task may be inside a blocking demux call on a worker
        # thread; the container must not be closed underneath it (libav
        # use-after-free). Wait for full task termination first.
        await asyncio.gather(*tasks, return_exceptions=True)
        self._stop_downlink_receiver()
        thread = self._downlink_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 5.0)
        if self._uplink_writer is not None:
            self._uplink_writer.close()
        if self.track is not None:
            track = self.track
            self.track = None
            await asyncio.to_thread(track.close)
        if self._ws is not None:
            await self._ws.close()
        await remove_attachment_view(self._attachment_view_dir)
        self._attachment_view_dir = None
        await self._http.close()
        self.session = None
        self._has_server_session = False
