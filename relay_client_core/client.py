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

log = logging.getLogger("relay.client")

# Media pumping is sized for a slow event loop, not a fast one: under qasync
# the loop shares the GUI thread with mpv rendering and turns come roughly
# once per painted frame (~25/s at 24 fps). Anything that needs a loop turn
# per packet caps out near half the frame rate — observed as the uplink
# starving the server below realtime while playback was smooth.
_UPLINK_BATCH = 16  # packets demuxed+sent per loop-turn pair
_DOWNLINK_BATCH = 8  # one Qt-loop wakeup per batch, not per lossless frame
_DOWNLINK_SOCKET_BUFFER = 4 * 1024 * 1024


class _ThreadBridgeQueue:
    """A bounded queue written by a socket thread and awaited by asyncio.

    The producer schedules one event-loop wakeup per batch. Queue storage and
    backpressure live behind a threading.Condition, so the media reader never
    calls asyncio.Queue methods from outside the event-loop thread.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int):
        self._loop = loop
        self._maxsize = maxsize
        self._items = collections.deque()
        self._condition = threading.Condition()
        self._available = asyncio.Event()
        self._closed = False

    def put_batch_from_thread(self, items: list[MediaPacket | None]) -> bool:
        if not items:
            return True
        with self._condition:
            while not self._closed and len(self._items) + len(items) > self._maxsize:
                self._condition.wait(timeout=0.25)
            if self._closed:
                return False
            self._items.extend(items)
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


class RelayClient:
    def __init__(self, host: str, port: int):
        self._loop = asyncio.get_running_loop()
        self.host = host
        self.port = port
        self._http = aiohttp.ClientSession()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self.capabilities: dict | None = None
        self.session: SessionInfo | None = None
        self.track: VideoTrack | None = None
        self.epoch = 0
        self.state = "idle"
        self._pending: dict[str, asyncio.Future] = {}
        self._down_q = _ThreadBridgeQueue(self._loop, maxsize=1024)
        self._uplink_writer: asyncio.StreamWriter | None = None
        self._uplink_task: asyncio.Task | None = None
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
        self.buffered_ms = 0  # consumer updates; buffer_report loop sends it

    # -- control channel -------------------------------------------------------

    async def connect(self) -> dict:
        self._ws = await self._http.ws_connect(f"http://{self.host}:{self.port}/control")
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

    async def _request(self, expect: str, type_: str, timeout: float = 30, **fields) -> dict:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[expect] = fut
        await self._send(type_, **fields)
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _control_reader(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            msg = json.loads(raw.data)
            mtype = msg.get("type")
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
            fut = self._pending.pop(mtype, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def open_session(self, cfg: SessionConfig) -> SessionInfo:
        if cfg.source not in ("uplink", "server_file"):
            raise ValueError(f"unknown session source: {cfg.source}")
        self.track = VideoTrack(cfg.path) if cfg.source == "uplink" else None
        video = self.track.open_session_video_dict() if self.track else None
        duration_s = self.track.duration_seconds() if self.track else None
        source = ("uplink" if cfg.source == "uplink" else
                  {"type": "server_file", "path": cfg.path})
        # Generous timeout: a cold TensorRT engine build at session open can
        # take a minute or two.
        fields = {
            "source": source,
            "file": {"name": cfg.path, "duration_s": duration_s},
            "model": cfg.model,
            "quality_tier": cfg.quality_tier,
            "display": {"w": cfg.display_w, "h": cfg.display_h},
            "fit_mode": cfg.fit_mode,
        }
        if cfg.resize_algorithm is not None:
            fields["resize_algorithm"] = cfg.resize_algorithm
        if video is not None:
            fields["video"] = video
        try:
            msg = await self._request(
                "session_opened", "open_session", timeout=240, **fields
            )
        except BaseException:
            if self.track is not None:
                self.track.close()
                self.track = None
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
        )
        return self.session

    async def fetch_library(self) -> dict:
        async with self._http.get(f"http://{self.host}:{self.port}/library") as response:
            response.raise_for_status()
            return (await response.json())["tree"]

    def media_url(self, relative_path: str) -> str:
        path = quote(relative_path, safe="/")
        return f"http://{self.host}:{self.port}/media/{path}"

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
        if epoch is None:
            epoch = self.epoch
        self._uplink_task = asyncio.create_task(self._uplink_loop(from_pts, discontinuity, epoch))

    async def _uplink_loop(self, from_pts: int | None, discontinuity: bool, epoch: int) -> None:
        assert self.track is not None and self._uplink_writer is not None
        first = True
        try:
            iterator = self.track.packets(from_pts)

            def next_batch() -> list:
                batch = []
                for info in iterator:
                    batch.append(info)
                    if len(batch) >= _UPLINK_BATCH:
                        break
                return batch

            while True:
                # One thread hop + one drain per batch: per-packet round-trips
                # need ~2 loop turns each and starve the server when the GUI
                # loop is slow (see _UPLINK_BATCH above).
                batch = await asyncio.to_thread(next_batch)
                if epoch != self.epoch:
                    return
                buf = bytearray()
                for info in batch:
                    pkt = self.track.media_packet(info, epoch,
                                                  discontinuity=discontinuity and first)
                    first = False
                    buf += encode_packet(pkt)
                if buf:
                    self._uplink_writer.write(bytes(buf))
                    await self._uplink_writer.drain()
                if len(batch) < _UPLINK_BATCH:  # iterator exhausted
                    break
            if epoch == self.epoch:
                self._uplink_writer.write(
                    encode_packet(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=epoch))
                )
                await self._uplink_writer.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError) as err:
            log.info("uplink closed: %r", err)

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
                epoch = self.epoch
                if batch and batch[0] is not None and batch[0].epoch < epoch:
                    batch.clear()
                if pkt.epoch < epoch:
                    continue
                batch.append(pkt)
                if len(batch) >= _DOWNLINK_BATCH or pkt.eos:
                    if not self._down_q.put_batch_from_thread(batch):
                        return
                    batch = []
        except (EOFError, OSError, ConnectionError, RuntimeError) as err:
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
        if self._ws is not None and not self._ws.closed:
            try:
                await self._send("teardown")
                await asyncio.sleep(0.1)
            except (ConnectionResetError, RuntimeError):
                pass
        await self.close()

    async def close(self) -> None:
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
            self.track.close()
        if self._ws is not None:
            await self._ws.close()
        await self._http.close()
