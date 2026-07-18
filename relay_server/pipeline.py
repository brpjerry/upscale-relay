"""Server-side media pipeline: uplink packets -> decode -> [upscale] -> [fit]
-> encode -> downlink packets.

Three worker threads (decode / inference / fit+encode) connected by bounded
queues so the CPU stages overlap the GPU ones -- the serial version capped
throughput at the *sum* of stage times instead of the max. PyAV, numpy and
onnxruntime all release the GIL during their heavy work.

Flush/seek (docs/PROTOCOL.md S4): the session drains `in_q` and enqueues a
_FlushCmd, which flows through all three stages in order; each stage resets
its own state (decoder / discard window / encoder). Stale-epoch items are
dropped at every stage boundary.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

import av

from relay_protocol import (
    FLAG_DISCONTINUITY,
    FLAG_EOS,
    FLAG_KEYFRAME,
    NO_TS,
    MediaPacket,
)
from upscale_cli.encode import DEFAULT_LOSSLESS_HEVC_PROFILE, select_encoder
from upscale_cli.fit import (
    DEFAULT_RESIZE_ALGORITHM,
    aligned_target_dimensions,
    cover_crop_box,
    fit_dimensions,
    interpolation_for_algorithm,
)

log = logging.getLogger("relay.pipeline")

# Top-off pacing: pause above HIGH, resume as soon as the buffer dips below
# RESUME. The gap is one buffer_report interval (~500 ms) — just enough to
# stop the pause flag flapping on every report, while keeping the client
# buffer pinned near HIGH instead of sawtoothing down to a low watermark.
HIGH_WATERMARK_MS = 10_000
RESUME_WATERMARK_MS = 9_500

_QUEUE_DEPTH = 4  # frames buffered between stages


@dataclass(slots=True)
class _FlushCmd:
    epoch: int
    target_pts: int


@dataclass(slots=True)
class _Eos:
    epoch: int


@dataclass(slots=True)
class _Frame:
    """A decoded/processed frame in flight between stages."""

    epoch: int
    pts: int | None
    frame: av.VideoFrame | None = None  # passthrough path
    rgb: object | None = None  # ndarray path (after inference)


class _SinkBuffer:
    """Write target for the output muxer. Deliberately has no seek/tell so
    the Matroska muxer runs in streaming (non-seekable) mode."""

    def __init__(self):
        self._chunks: list[bytes] = []

    def write(self, data) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def drain(self) -> bytes:
        if not self._chunks:
            return b""
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


@dataclass
class VideoConfig:
    codec: str
    extradata: bytes | None
    width: int
    height: int
    time_base: Fraction
    avg_rate: Fraction | None = None


@dataclass
class PipelineStats:
    frames_in: int = 0
    frames_out: int = 0
    last_pts_out: int = NO_TS
    fps: float = 0.0
    paused_for_backpressure: bool = False
    stage_ms: dict = field(default_factory=dict)  # stage -> [count, total_ms]
    _window_start: float = field(default_factory=time.perf_counter)
    _window_frames: int = 0

    def add_stage_time(self, stage: str, ms: float) -> None:
        entry = self.stage_ms.setdefault(stage, [0, 0.0])
        entry[0] += 1
        entry[1] += ms

    def stage_report(self) -> dict[str, float]:
        return {s: round(t / c, 1) for s, (c, t) in self.stage_ms.items() if c}

    def tick_out(self, pts: int) -> None:
        self.frames_out += 1
        self.last_pts_out = pts
        self._window_frames += 1
        now = time.perf_counter()
        if now - self._window_start >= 2.0:
            self.fps = self._window_frames / (now - self._window_start)
            self._window_start = now
            self._window_frames = 0


class Pipeline:
    """One per session. Feed via in_q; results arrive via emit callback."""

    def __init__(
        self,
        video: VideoConfig,
        model_path: str | None,
        quality_tier: str,
        display: tuple[int, int],
        emit: Callable[[MediaPacket], None],
        on_error: Callable[[str], None],
        ep: str = "auto",
        fit_mode: str = "fit",
        resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM,
        lossless_hevc_profile: str = DEFAULT_LOSSLESS_HEVC_PROFILE,
    ):
        self.video = video
        self.emit = emit
        self.on_error = on_error
        self.stats = PipelineStats()
        self.in_q: queue.Queue = queue.Queue(maxsize=256)
        self._q_dec: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._q_up: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)

        self.upscaler = None
        scale = 1
        if model_path:
            # Fixed tiling decision from known input dims: no "auto" probing
            # (which shells out to nvidia-smi) in the streaming hot path.
            tile = None if (video.height <= 1440 and video.width <= 2560) else 1024
            import onnxruntime as _ort

            use_trt = ep == "tensorrt" or (
                ep == "auto" and "TensorrtExecutionProvider" in _ort.get_available_providers()
            )
            if use_trt:
                # The ORT TensorRT EP corrupts the process heap on this stack
                # (see upscale_cli/infer_worker.py) — run it out-of-process.
                from upscale_cli.infer_worker import SubprocessUpscaler

                self.upscaler = SubprocessUpscaler(model_path, ep="tensorrt", tile_size=tile)
            else:
                from upscale_cli.infer import OnnxUpscaler

                self.upscaler = OnnxUpscaler(model_path, ep=ep, tile_size=tile)
            if self.upscaler.scale_factor is None:
                raise ValueError(f"model {model_path} needs a manifest with scale_factor")
            scale = self.upscaler.scale_factor

        # "fit" preserves the full image inside the display. "cover" crops the
        # post-ONNX frame centrally before resizing to the display dimensions.
        if fit_mode not in ("fit", "cover"):
            raise ValueError(f"unknown fit mode {fit_mode!r}")
        self.fit_mode = fit_mode
        self.resize_algorithm = resize_algorithm
        self._interpolation = interpolation_for_algorithm(resize_algorithm)
        processed_w, processed_h = video.width * scale, video.height * scale
        if fit_mode == "cover":
            self.out_w, self.out_h = aligned_target_dimensions(*display)
            self._crop_box = cover_crop_box(
                processed_w, processed_h, self.out_w, self.out_h,
            )
        else:
            self.out_w, self.out_h = fit_dimensions(
                processed_w, processed_h, display[0], display[1]
            )
            self._crop_box = None

        self.lossless_hevc_profile = lossless_hevc_profile
        self.quality_tier = quality_tier
        self._enc_codec, self._enc_pix_fmt, self._enc_options = select_encoder(
            quality_tier, lossless_hevc_profile=lossless_hevc_profile,
        )
        self.encoder_name = self._enc_codec
        self._mux = None
        self._enc_stream = None
        self._sink_buf = _SinkBuffer()
        self._open_mux()
        self.downlink_container = "matroska"
        self.downlink_codec = {
            "hevc_nvenc": "hevc", "libx265": "hevc", "libx264": "h264",
        }.get(self._enc_codec, self._enc_codec)
        self.downlink_extradata_b64 = None  # container is self-describing

        # Persistent reformatters: swscale context setup (filter tables) is
        # expensive at multi-megapixel sizes; rebuild-per-frame costs ~5x more
        # than the conversion itself (worst with 10-bit sources). One instance
        # per stage thread -- they are not thread-safe.
        self._reformatter = av.video.reformatter.VideoReformatter()  # finish thread
        self._crop_reformatter = av.video.reformatter.VideoReformatter()  # finish thread
        self._in_reformatter = av.video.reformatter.VideoReformatter()  # infer thread
        self._decoder = self._open_decoder()
        self._epoch = 0  # decode-stage epoch (authoritative for stale drops)
        self._discard_until: int | None = None
        self._need_discontinuity = True
        self.client_buffered_ms = 0  # last buffer_report value (raw)
        self._client_buffered_at = time.monotonic()
        self.playing = False  # session mirrors PLAYING state here
        self._flush_pending = threading.Event()
        self._closed = threading.Event()
        self._threads = [
            threading.Thread(target=self._guard(self._decode_work), daemon=True, name="pl-decode"),
            threading.Thread(target=self._guard(self._infer_work), daemon=True, name="pl-infer"),
            threading.Thread(target=self._guard(self._finish_work), daemon=True, name="pl-finish"),
        ]
        for t in self._threads:
            t.start()

    # -- codec management ----------------------------------------------------

    def _open_decoder(self) -> av.CodecContext:
        # NVDEC is OPT-IN (RELAY_NVDEC=1): running NVDEC decode concurrently
        # with NVENC encode in this process crashed with a native AV in the
        # decode thread (field report, lossless-hevc tier). Software decode
        # is ~6 ms/frame and was never the pipeline bottleneck.
        import os

        if os.environ.get("RELAY_NVDEC") and not getattr(self, "_hw_decode_failed", False):
            try:
                from av.codec.hwaccel import HWAccel

                ctx = av.CodecContext.create(
                    self.video.codec, "r",
                    hwaccel=HWAccel(device_type="cuda", allow_software_fallback=False),
                )
                if self.video.extradata:
                    ctx.extradata = self.video.extradata
                return ctx
            except Exception:
                self._hw_decode_failed = True
                log.info("NVDEC unavailable for %s; using software decode", self.video.codec)
        ctx = av.CodecContext.create(self.video.codec, "r")
        if self.video.extradata:
            ctx.extradata = self.video.extradata
        # Threaded software decode is OPT-IN (RELAY_DECODE_THREADS=1): with
        # PyAV 18's bundled ffmpeg 8, frame-threaded decode under sustained
        # paced streaming crashes with a heap read-AV inside avcodec-62
        # (captured by relay_server.crashinfo). Single-threaded decode is
        # ~15-25 ms for 10-bit HEVC 1080p — within pipeline budget.
        if os.environ.get("RELAY_DECODE_THREADS"):
            ctx.thread_count = 0
            ctx.thread_type = "AUTO"
        else:
            ctx.thread_count = 1
            ctx.thread_type = "NONE"
        return ctx

    def _safe_put(self, q: queue.Queue, item) -> bool:
        """Bounded-queue put that never deadlocks a closing pipeline: wakes
        every 200 ms to re-check _closed. A put blocked forever on a dead
        consumer left zombie threads (and their codec state) alive across
        sessions."""
        while not self._closed.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _open_mux(self) -> None:
        """(Re)create the output container + encoder stream. One container per
        epoch: its byte stream is the downlink payload (docs/PROTOCOL.md S3.2)."""
        if self._mux is not None:
            try:
                self._mux.close()
            except Exception:
                pass
            self._sink_buf.drain()  # discard any trailer of the abandoned epoch
        options = dict(self._enc_options)
        # Frequent keyframes keep post-seek startup fast on the client.
        options.setdefault("g", "48")
        self._mux = av.open(
            self._sink_buf, mode="w", format="matroska",
            container_options={"cluster_time_limit": "100"},
        )
        self._enc_stream = self._mux.add_stream(
            self._enc_codec, rate=self.video.avg_rate, options=options
        )
        self._enc_stream.width = self.out_w
        self._enc_stream.height = self.out_h
        self._enc_stream.pix_fmt = self._enc_pix_fmt

    # -- public API (called from asyncio thread) ------------------------------

    def feed(self, pkt: MediaPacket) -> None:
        """Blocking put -- caller runs it in an executor for natural backpressure."""
        self.in_q.put(pkt)

    def note_buffer_report(self, buffered_ms: int) -> None:
        self.client_buffered_ms = buffered_ms
        self._client_buffered_at = time.monotonic()

    def buffered_ms_now(self) -> float:
        """Estimate of the client buffer right now: the last report, decayed
        by wall time while playing (the client consumes in real time).
        Reports can stall or stop entirely — a busy client event loop, a
        saturated link, a dead reporter task — and pacing on the raw last
        value wedged the backpressure pause while the real buffer drained
        to zero. If reports stop for good the estimate floors out and the
        pipeline free-runs; the down_q gate in Session bounds that."""
        ms = float(self.client_buffered_ms)
        if self.playing:
            ms -= (time.monotonic() - self._client_buffered_at) * 1000.0
        return ms

    def flush(self, epoch: int, target_pts: int) -> None:
        """Drain pending input and schedule a reset. Called on seek."""
        self._flush_pending.set()  # breaks the backpressure wait immediately
        self._epoch = epoch  # downstream stages start dropping stale items now
        try:
            while True:
                self.in_q.get_nowait()
        except queue.Empty:
            pass
        self.in_q.put(_FlushCmd(epoch=epoch, target_pts=target_pts))

    def close(self) -> None:
        self._closed.set()
        try:
            while True:
                self.in_q.get_nowait()
        except queue.Empty:
            pass
        self.in_q.put(None)
        if self.upscaler is not None and hasattr(self.upscaler, "close"):
            try:
                self.upscaler.close()
            except Exception:
                pass

    @property
    def epoch(self) -> int:
        return self._epoch

    def queue_depths(self) -> dict[str, int]:
        return {"in": self.in_q.qsize(), "decoded": self._q_dec.qsize(), "upscaled": self._q_up.qsize()}

    # -- worker threads -----------------------------------------------------------

    def _guard(self, fn):
        downstream = {"_decode_work": self._q_dec, "_infer_work": self._q_up}.get(fn.__name__)

        def run():
            try:
                fn()
            except Exception as err:  # pragma: no cover - defensive
                if not self._closed.is_set():
                    log.exception("pipeline stage %s failed", fn.__name__)
                    self.on_error(f"pipeline: {err!r}")
            finally:
                if downstream is not None:
                    try:
                        downstream.put_nowait(None)  # make sure the next stage exits
                    except queue.Full:
                        pass
        return run

    # Stage 1: packets -> decoded frames (+ discard window + backpressure)

    def _decode_work(self) -> None:
        while not self._closed.is_set():
            item = self.in_q.get()
            if item is None:
                self._q_dec.put(None)
                return
            if isinstance(item, _FlushCmd):
                self._decoder = self._open_decoder()
                self._discard_until = item.target_pts
                self._flush_pending.clear()
                self.stats.paused_for_backpressure = False
                self._safe_put(self._q_dec, item)
                continue

            pkt: MediaPacket = item
            if pkt.epoch < self._epoch:
                continue
            if pkt.eos:
                for frame in self._decoder.decode(None):
                    self._put_decoded(frame, pkt.epoch)
                self._safe_put(self._q_dec, _Eos(epoch=pkt.epoch))
                self._decoder = self._open_decoder()  # spent after drain
                continue

            # Backpressure: keep the client buffer topped off (docs/PROTOCOL.md S5).
            if self.buffered_ms_now() > HIGH_WATERMARK_MS:
                self.stats.paused_for_backpressure = True
                while (
                    self.buffered_ms_now() > RESUME_WATERMARK_MS
                    and not self._closed.is_set()
                    and not self._flush_pending.is_set()
                ):
                    time.sleep(0.05)
                self.stats.paused_for_backpressure = False

            t0 = time.perf_counter()
            # av.Packet(bytes) in PyAV >= 18 wraps the bytes buffer zero-copy
            # WITHOUT ffmpeg's required AV_INPUT_BUFFER_PADDING_SIZE; decoder
            # bitstream readers overread past size by design, so payloads
            # ending near a page boundary crash with a read-AV inside
            # avcodec (captured at avcodec-62+0x351118, intermittent).
            # Packet(size) uses av_new_packet, which allocates padded and
            # zeroed; update() copies the payload in.
            av_pkt = av.Packet(len(pkt.payload))
            av_pkt.update(pkt.payload)
            av_pkt.pts = pkt.pts if pkt.pts != NO_TS else None
            av_pkt.dts = pkt.dts if pkt.dts != NO_TS else None
            av_pkt.time_base = self.video.time_base
            self.stats.frames_in += 1
            try:
                frames = self._decoder.decode(av_pkt)
            except Exception:
                if getattr(self, "_hw_decode_failed", False):
                    raise
                # NVDEC opened but failed at runtime: retry in software.
                self._hw_decode_failed = True
                log.info("NVDEC decode failed at runtime; falling back to software")
                self._decoder = self._open_decoder()
                frames = self._decoder.decode(av_pkt)
            self.stats.add_stage_time("decode", (time.perf_counter() - t0) * 1000)
            for frame in frames:
                self._put_decoded(frame, pkt.epoch)

    _HW_PIX_FMTS = {"cuda", "d3d11", "d3d11va_vld", "vaapi", "qsv"}

    def _put_decoded(self, frame: av.VideoFrame, epoch: int) -> None:
        if frame.pts is not None and self._discard_until is not None:
            if frame.pts < self._discard_until:
                return
            self._discard_until = None
        if frame.format.name in self._HW_PIX_FMTS:
            # Download NVDEC frames on the decode thread (parallel with infer).
            cpu = frame.reformat(format="nv12")
            cpu.pts = frame.pts
            cpu.time_base = frame.time_base
            frame = cpu
        self._safe_put(self._q_dec, _Frame(epoch=epoch, pts=frame.pts, frame=frame))

    # Stage 2: decoded frames -> upscaled rgb (GPU inference)

    def _infer_work(self) -> None:
        while not self._closed.is_set():
            item = self._q_dec.get()
            if item is None:
                self._q_up.put(None)
                return
            if isinstance(item, (_FlushCmd, _Eos)):
                self._safe_put(self._q_up, item)
                continue
            if item.epoch < self._epoch:
                continue
            if self.upscaler is not None:
                t0 = time.perf_counter()
                frame = item.frame
                if frame.format.name != "rgb24":
                    # Cached swscale context; frame.to_ndarray would rebuild
                    # one per frame (ruinous for 10-bit sources).
                    frame = self._in_reformatter.reformat(frame, format="rgb24")
                rgb = frame.to_ndarray(format="rgb24")
                item.rgb = self.upscaler._infer_with_fallback(rgb)
                item.frame = None
                self.stats.add_stage_time("infer", (time.perf_counter() - t0) * 1000)
            self._safe_put(self._q_up, item)

    # Stage 3: upscaled frames -> fit -> encode -> downlink packets

    def _finish_work(self) -> None:
        while not self._closed.is_set():
            item = self._q_up.get()
            if item is None:
                return
            if isinstance(item, _FlushCmd):
                self._open_mux()  # abandon the old epoch's container
                self._need_discontinuity = True
                continue
            if isinstance(item, _Eos):
                if item.epoch < self._epoch:
                    continue
                # Drain encoder, finalize the container, ship the trailer.
                self._mux.mux(self._enc_stream.encode(None))
                self._mux.close()
                self._mux = None
                self._flush_chunk(item.epoch, NO_TS, keyframe=False)
                self.emit(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=item.epoch))
                self._open_mux()  # ready in case another epoch follows
                continue
            if item.epoch < self._epoch:
                continue

            t0 = time.perf_counter()
            if item.rgb is not None:
                rgb = item.rgb
            elif self._crop_box is not None:
                # Inferred frames are already RGB. Passthrough+cover needs one
                # conversion before the array crop; keep its swscale context
                # separate from the final scale/format conversion.
                crop_source = item.frame
                if crop_source.format.name != "rgb24":
                    crop_source = self._crop_reformatter.reformat(crop_source, format="rgb24")
                rgb = crop_source.to_ndarray(format="rgb24")
            else:
                rgb = None
            if rgb is not None:
                if self._crop_box is not None:
                    x, y, crop_w, crop_h = self._crop_box
                    rgb = rgb[y:y + crop_h, x:x + crop_w]
                out = av.VideoFrame.from_ndarray(rgb, format="rgb24")
            else:
                out = item.frame
            if (out.width, out.height, out.format.name) != (self.out_w, self.out_h, self._enc_pix_fmt):
                # Single swscale pass: scale + pixel format together.
                out = self._reformatter.reformat(
                    out, width=self.out_w, height=self.out_h,
                    format=self._enc_pix_fmt, interpolation=self._interpolation,
                )
            out.pts = item.pts
            out.time_base = self.video.time_base
            t1 = time.perf_counter()
            self.stats.add_stage_time("fit", (t1 - t0) * 1000)
            keyframe = False
            for av_pkt in self._enc_stream.encode(out):
                keyframe = keyframe or bool(av_pkt.is_keyframe)
                self._mux.mux(av_pkt)
            self.stats.add_stage_time("encode", (time.perf_counter() - t1) * 1000)
            self.stats.tick_out(item.pts if item.pts is not None else NO_TS)
            self._flush_chunk(item.epoch, item.pts, keyframe)

    def _flush_chunk(self, epoch: int, pts: int | None, keyframe: bool) -> None:
        """Ship whatever container bytes the muxer has produced so far."""
        data = self._sink_buf.drain()
        if not data:
            return
        flags = FLAG_KEYFRAME if keyframe else 0
        if self._need_discontinuity:
            flags |= FLAG_DISCONTINUITY
            self._need_discontinuity = False
        self.emit(MediaPacket(
            payload=data,
            flags=flags,
            epoch=epoch,
            pts=pts if pts is not None else NO_TS,
            dts=NO_TS,
        ))
