"""Display-mapped HDR (docs/PROTOCOL.md §3.6).

FFmpeg's libplacebo filter, on Vulkan, applies a source's Dolby Vision RPU or
HDR10+ messages for the client's display. It runs in an ``ffmpeg`` subprocess
per epoch, which stands in for the pipeline's decoder: the decode stage hands
it the source packets and it returns mapped 10-bit frames. Like TensorRT's
worker it keeps a second GPU stack out of the process that runs NVENC.

- Both directions are NUT over localhost TCP. NUT keeps the source's
  timestamps exactly (Matroska would round them to milliseconds). TCP
  because a Windows anonymous pipe passes a few KB per read: 4K frames moved
  at ~1 fps through one.
- The packets go in as Annex B with the parameter sets in band: PyAV cannot
  give a new stream the source's hvcC. A Dolby Vision RPU is decoded and
  applied without its configuration record.
- NUT carries no colour description, so ffmpeg's frames have only the
  bitstream's. Tags it leaves unspecified are filled from the container's,
  as the pipeline's own decode path fills them (``container_fill``).

FFmpeg's filter cannot be given a target peak (it takes the target's
luminance from the source frame, in 9.0 and master alike), so a PQ target is
mapped only for a source with no displayable base layer (Dolby Vision
profile 5), at its own brightness. SDR targets get the full mapping.
"""

from __future__ import annotations

import collections
import logging
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

import av

from relay_media.hdr import DolbyVisionConfig, hevc_nal_length_size, hevc_parameter_sets, to_annexb
from upscale_cli.color import fill_unspecified

log = logging.getLogger("relay.display_mapping")

FFMPEG_ENV = "RELAY_FFMPEG"
TRANSFERS = ("pq", "sdr")
# (matrix, range, primaries, transfer) of a mapped epoch (upscale_cli.color).
MAPPED_TAGS = {"sdr": (1, 1, 1, 1), "pq": (9, 1, 9, 16)}
# Profiles whose RPU libplacebo applies: 7's enhancement layer is not carried.
_DOVI_PROFILES = frozenset({5, 7, 8})
_DOVI_SDR_BASE = 2  # bl_signal_compatibility_id of 8.2: the base layer is SDR
_PROBE_TIMEOUT_S = 30.0  # all of detect()'s probes together
_CONNECT_TIMEOUT_S = 20.0
# End of stream: how long ffmpeg may go without a frame out. Time spent
# handing frames on is not counted (a paused client holds them up at will).
_FINISH_IDLE_S = 30.0
_KILL_TIMEOUT_S = 5.0
# Auxiliary items wait for the video frames written before them, except
# when for _IDLE_S no packet went to ffmpeg and nothing came back: then it
# waits for packets not yet written, and what is held goes on. Otherwise
# audio that runs on after a source's last video packet would be held, all
# of it, until the end of the stream. hold() waits beyond _HOLD_MAX items,
# which stops the writes and so lets the reader release them.
_IDLE_S = 0.25
_HOLD_MAX = 4096
_READ_CHUNK = 1 << 20
_SOCKET_BUFFER = 1 << 24  # frames out: 4K 10-bit is 25 MB each
# Packets in: what is written but not yet mapped. A seek kills the mapper
# rather than waiting for it, but a small buffer also keeps less queued.
_INPUT_BUFFER = 1 << 21
_STDERR_LINES = 20
# NUT's muxer wants every packet's DTS, increasing and at most its PTS, and
# sources often have none (Matroska with B-frames); the decoder ignores them.
# So each packet gets the next tick of a decode clock that starts _DTS_LEAD
# ticks before the epoch's first PTS, and both are lifted by _TS_OFFSET to
# stay positive. The mapper's output comes back down by _TS_OFFSET.
_TS_OFFSET = 1 << 40
_DTS_LEAD = 1 << 39


@dataclass(frozen=True)
class DisplayTarget:
    """``display_target``: the display a client presents on."""

    transfer: str
    peak_nits: float | None = None
    sdr_white_nits: float | None = None

    @classmethod
    def from_wire(cls, value) -> "DisplayTarget | None":
        """None for anything but a dict naming a known transfer."""
        if not isinstance(value, dict) or value.get("transfer") not in TRANSFERS:
            return None

        def nits(key: str) -> float | None:
            raw = value.get(key)
            if type(raw) not in (int, float) or not math.isfinite(raw) or not 0 < raw <= 10000:
                return None
            return float(raw)

        return cls(value["transfer"], nits("peak_nits"), nits("sdr_white_nits"))


@dataclass(frozen=True)
class SourceDynamics:
    """What a source carries that a display mapping applies."""

    codec: str
    dolby_vision: DolbyVisionConfig | None = None
    hdr10plus: bool = False

    def applied_for(self, transfer: str) -> tuple[str, ...]:
        """The dynamic metadata a ``transfer`` target has applied (§3.6's
        table); empty: the epoch streams as §3.5 says."""
        if self.codec != "hevc":
            return ()
        dovi = self.dolby_vision
        if transfer == "pq":
            return ("dolby_vision",) if dovi is not None and dovi.profile == 5 else ()
        if dovi is not None and dovi.profile in _DOVI_PROFILES and not (
            dovi.profile == 8 and dovi.bl_signal_compatibility_id == _DOVI_SDR_BASE
        ):
            return ("dolby_vision",)
        return ("hdr10plus",) if self.hdr10plus else ()

    @property
    def mappable(self) -> bool:
        return any(self.applied_for(transfer) for transfer in TRANSFERS)


@dataclass(frozen=True)
class EpochMapping:
    """How one epoch of a mapped session is made (``downlink_mapping``)."""

    transfer: str
    applied: tuple[str, ...]

    @property
    def mapped(self) -> bool:
        return bool(self.applied)

    @property
    def tags(self) -> tuple[int, int, int, int]:
        return MAPPED_TAGS[self.transfer]

    def to_wire(self) -> dict:
        # Neither luminance is fitted to the client's (module docstring).
        return {
            "transfer": self.transfer, "peak_nits": None, "sdr_white_nits": None,
            "applied": list(self.applied),
        }

    def filter_graph(self) -> str:
        """The libplacebo filter that makes this epoch. The dynamic metadata
        drives the tone curve: no peak detection, contrast recovery or other
        libplacebo enhancement."""
        if self.transfer == "sdr":
            space = "colorspace=bt709:color_primaries=bt709:color_trc=bt709"
        else:
            space = "colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084"
        dovi = "dolby_vision" in self.applied
        curve = "st2094-10" if dovi else "st2094-40"
        metadata = "cie_y" if dovi else "hdr10plus"  # Dolby Vision's L1 is CIE Y
        return (
            f"libplacebo={space}:range=limited:format=yuv420p10le"
            f":apply_dolbyvision={int(dovi)}:tonemapping={curve}"
            ":peak_detect=0:contrast_recovery=0"
            f":extra_opts='tone_map_metadata={metadata}'"
        )


def decide(
    source: SourceDynamics, target: DisplayTarget | None,
) -> EpochMapping | None:
    """The mapping of an epoch for ``target``; None on a session that is
    never mapped (no target, or a source mapped for none)."""
    if target is None or not source.mappable:
        return None
    return EpochMapping(target.transfer, source.applied_for(target.transfer))


# setparams' option for each of (matrix, range, primaries, transfer).
_SETPARAMS = ("colorspace", "range", "color_primaries", "color_trc")


def container_fill(decoded, container) -> str | None:
    """A ``setparams`` filter giving the mapper's frames the container's
    colour tags where the bitstream leaves them unspecified; None when it
    leaves nothing the container fills.

    ``decoded`` is what the source's decoder exports from its parameter sets
    (None: unknown, as unspecified), ``container`` the session's tags (None:
    none). Tags the bitstream sets are never touched. An alternative
    transfer SEI over a VUI with no transfer of its own is the one case a
    filter cannot tell apart: the container's transfer wins there.
    """
    if container is None:
        return None
    decoded = tuple(decoded) if decoded is not None else (2, 0, 2, 2)
    filled = fill_unspecified(decoded, container)
    options = [f"{name}={value}" for name, value, was in zip(_SETPARAMS, filled, decoded)
               if value != was]
    return "setparams=" + ":".join(options) if options else None


def _child_environment() -> dict[str, str]:
    env = dict(os.environ)
    # Implicit Vulkan layers are overlays and capture hooks (Steam, OBS,
    # GOG Galaxy, Epic, ASUS on the server box): nothing a headless filter
    # needs, and loading them is most of libplacebo's own device creation.
    env.setdefault("VK_LOADER_LAYERS_DISABLE", "~implicit~")
    return env


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class DisplayMapper:
    """An ``ffmpeg`` whose libplacebo filter works on this machine."""

    def __init__(self, ffmpeg: str):
        self.ffmpeg = ffmpeg

    @classmethod
    def detect(cls, ffmpeg: str | None = None) -> "DisplayMapper | None":
        """``ffmpeg`` (a path), else $RELAY_FFMPEG, else ``ffmpeg`` on PATH,
        if it can map a frame; None otherwise (blocking: up to
        _PROBE_TIMEOUT_S, 30 s, for all of its probes)."""
        candidate = ffmpeg or os.environ.get(FFMPEG_ENV) or shutil.which("ffmpeg")
        if not candidate:
            log.info("display mapping off: no ffmpeg (pass --ffmpeg or set %s)", FFMPEG_ENV)
            return None
        mapper = cls(candidate)
        problem = mapper.probe()
        if problem is not None:
            log.warning("display mapping off: %s cannot map: %s", candidate, problem)
            return None
        log.info("display mapping on: %s (libplacebo on Vulkan)", candidate)
        return mapper

    def base_command(self) -> list[str]:
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            # The device is made before the input opens, so a spare process
            # has it ready, and libplacebo's own is slower to create.
            "-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk",
        ]

    def probe(self) -> str | None:
        """Map one tiny frame through each filter graph a session may use;
        None when it works, else what went wrong."""
        graphs = {EpochMapping("sdr", ("dolby_vision",)).filter_graph(),
                  EpochMapping("sdr", ("hdr10plus",)).filter_graph(),
                  EpochMapping("pq", ("dolby_vision",)).filter_graph()}
        deadline = time.monotonic() + _PROBE_TIMEOUT_S
        for graph in sorted(graphs):
            command = self.base_command() + [
                "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=0.04,format=yuv420p10le",
                # A container's tags as container_fill sets them.
                "-vf", f"setparams=color_primaries=9:color_trc=16,{graph}", "-f", "null", "-",
            ]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"the probes took over {_PROBE_TIMEOUT_S:.0f} s"
            try:
                done = subprocess.run(
                    command, capture_output=True, timeout=remaining,
                    env=_child_environment(), creationflags=_creation_flags(),
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return str(error)
            if done.returncode != 0:
                said = done.stderr.decode(errors="replace").strip().splitlines()
                return said[-1] if said else f"exit status {done.returncode}"
        return None

    def spawn(self, mapping: EpochMapping, *, codec: str, extradata: bytes | None,
              width: int, height: int, time_base: Fraction,
              prefilter: str | None = None) -> "MapperProcess":
        return MapperProcess(self, mapping, codec=codec, extradata=extradata,
                             width=width, height=height, time_base=time_base,
                             prefilter=prefilter)


def _listener() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _SOCKET_BUFFER)
    sock.bind(("127.0.0.1", 0))  # the kernel's port: nothing to race for
    sock.listen(1)
    return sock


class _SocketReader:
    """Read side of a socket for PyAV: no seek or tell, so the demuxer reads
    the stream as it arrives. ``on_idle`` runs after each _IDLE_S without
    data (the socket's timeout); it returns False to end the stream."""

    def __init__(self, sock: socket.socket, on_idle: Callable[[], bool]):
        self._sock = sock
        self._on_idle = on_idle

    def read(self, size: int) -> bytes:
        while True:
            try:
                return self._sock.recv(min(size, _READ_CHUNK))
            except socket.timeout:
                if not self._on_idle():
                    return b""


class _SocketWriter:
    def __init__(self, sock: socket.socket):
        self._sock = sock

    def write(self, data) -> int:
        self._sock.sendall(data)
        return len(data)


class MapperError(RuntimeError):
    """The mapping subprocess failed; the message ends with what it said."""


class MapperProcess:
    """One epoch's mapping: an ``ffmpeg`` started at once (its Vulkan device
    ready for when packets come) and fed by one thread, the decode stage.

    ``start`` begins the epoch: a reader thread delivers each mapped frame,
    in presentation order with the source's timestamps and time base, to
    ``on_frame``, and auxiliary items held with ``hold`` once the frames
    before them are out. ``finish`` drains it (end of stream), ``kill``
    abandons it (seek, close; any thread). A failure is reported once:
    by the reader through ``on_error``, or by whoever calls
    ``claim_failure`` first.

    ``prefilter`` (``container_fill``) runs ahead of the mapping.
    """

    def __init__(self, mapper: DisplayMapper, mapping: EpochMapping, *, codec: str,
                 extradata: bytes | None, width: int, height: int, time_base: Fraction,
                 prefilter: str | None = None):
        self.mapping = mapping
        self._codec, self._width, self._height = codec, width, height
        self._time_base = Fraction(time_base)
        self._length_size = hevc_nal_length_size(extradata)
        self._parameter_sets = hevc_parameter_sets(extradata)
        self._killed = threading.Event()
        self._finishing = False
        self._started = False
        self._failure_claimed = False
        self._lock = threading.Lock()
        self._released = threading.Condition(self._lock)  # held items went on
        self._held: collections.deque = collections.deque()  # (after_pts, item)
        self._sent_pts: int | None = None  # highest video PTS written
        self._sent = 0  # packets written
        self._dts_base: int | None = None
        self._last_pts: int | None = None  # last frame delivered (reader)
        # The reader's last frame out, and whether it is handing one on now;
        # the decode thread's last packet in.
        self._progress_at = self._written_at = time.monotonic()
        self._delivering = False
        self._stderr: collections.deque[str] = collections.deque(maxlen=_STDERR_LINES)
        self._in_listener, self._out_listener = _listener(), _listener()
        self._in_conn: socket.socket | None = None
        self._writer = None
        self._stream = None
        self._reader: threading.Thread | None = None
        self.frames_out = 0
        graph = mapping.filter_graph() if prefilter is None else f"{prefilter},{mapping.filter_graph()}"
        command = mapper.base_command() + [
            "-f", "nut", "-i", f"tcp://127.0.0.1:{self._in_listener.getsockname()[1]}",
            "-map", "0:v:0", "-vf", graph,
            # Every frame, at its own timestamp, in the demuxer's time base.
            "-fps_mode", "passthrough", "-copyts", "-enc_time_base:v", "demux",
            "-c:v", "rawvideo", "-f", "nut",
            f"tcp://127.0.0.1:{self._out_listener.getsockname()[1]}",
        ]
        self.command = command
        try:
            self._proc = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=_child_environment(), creationflags=_creation_flags(),
            )
        except OSError as error:
            self._in_listener.close()
            self._out_listener.close()
            raise MapperError(f"display mapping: cannot run {mapper.ffmpeg}: {error}") from error
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="pl-mapper-stderr",
        )
        self._stderr_thread.start()

    # -- decode thread --------------------------------------------------------

    def start(self, on_frame: Callable[[av.VideoFrame], None],
              on_item: Callable[[object], None], on_error: Callable[[str], None]) -> None:
        """Begin the epoch. ``on_frame``/``on_item`` run on the reader thread,
        ``on_error`` there too when the subprocess fails mid-epoch."""
        self._on_frame, self._on_item, self._on_error = on_frame, on_item, on_error
        self._started = True
        # A seek that kills the mapper meanwhile closes these sockets: every
        # step fails as a MapperError, which the pipeline knows to expect.
        try:
            self._in_listener.settimeout(_CONNECT_TIMEOUT_S)
            self._in_conn, _ = self._in_listener.accept()
            self._in_conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, _INPUT_BUFFER)
            self._writer = av.open(_SocketWriter(self._in_conn), mode="w", format="nut")
            self._stream = self._writer.add_mux_stream(
                self._codec, width=self._width, height=self._height, time_base=self._time_base,
            )
        except Exception as error:
            raise self._failure(f"ffmpeg did not connect: {error!r}") from error
        finally:
            self._in_listener.close()
        self._reader = threading.Thread(target=self._read, daemon=True, name="pl-mapper-read")
        self._reader.start()

    def send(self, payload: bytes, pts: int | None, keyframe: bool) -> None:
        """Write one source access unit (decode thread)."""
        if self._killed.is_set():  # its socket is gone: never touch the muxer
            raise self._failure("ffmpeg was stopped")
        data = to_annexb(payload, self._length_size,
                         self._parameter_sets if keyframe or not self._sent else ())
        packet = av.Packet(len(data))  # padded, as the decoder needs
        packet.update(data)
        if self._dts_base is None:
            self._dts_base = (pts or 0) + _TS_OFFSET - _DTS_LEAD
        packet.pts = pts + _TS_OFFSET if pts is not None else None
        packet.dts = self._dts_base + self._sent
        self._sent += 1
        packet.time_base = self._time_base
        packet.is_keyframe = keyframe
        packet.stream = self._stream
        try:
            self._writer.mux(packet)
        except Exception as error:
            raise self._failure(f"writing to ffmpeg failed: {error!r}") from error
        self._written_at = time.monotonic()
        if pts is not None:
            with self._lock:
                self._sent_pts = pts if self._sent_pts is None else max(self._sent_pts, pts)

    def hold(self, item) -> None:
        """Deliver ``item`` (an auxiliary packet) after the frames of the
        packets written before it, so the muxer gets it in time order. Only
        the reader delivers, keeping held items in their own order: with
        the next frame out, once ffmpeg goes idle, or at the end of the
        stream. Beyond _HOLD_MAX items this waits for it, which cannot
        deadlock: no packet is written meanwhile, so the reader soon finds
        ffmpeg idle and releases them all."""
        with self._lock:
            while len(self._held) >= _HOLD_MAX and not self._killed.is_set():
                self._released.wait(0.2)
            self._held.append((self._sent_pts, item))

    def finish(self) -> None:
        """End of stream: every frame and held item is delivered on return.
        Fails when ffmpeg goes _FINISH_IDLE_S without a frame out."""
        self._finishing = True
        self._progress_at = time.monotonic()
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.close()  # the NUT trailer
                self._in_conn.shutdown(socket.SHUT_WR)
            except Exception as error:
                self.close()
                raise self._failure(f"closing ffmpeg's input failed: {error!r}") from error
        reader = self._reader
        while reader is not None and reader.is_alive() and not self._killed.is_set():
            reader.join(_IDLE_S)
            if (reader.is_alive() and not self._delivering
                    and time.monotonic() - self._progress_at > _FINISH_IDLE_S):
                self.close()
                raise self._failure("ffmpeg did not finish")
        self.close()

    def claim_failure(self) -> bool:
        """True for the first to report this mapper's failure (any thread):
        the reader's report and the decode thread's own error are one."""
        with self._lock:
            first, self._failure_claimed = not self._failure_claimed, True
        return first

    def close(self) -> None:
        """Abandon the epoch (decode thread): on return its reader has
        delivered its last frame and the subprocess is gone. Idempotent."""
        self.kill()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(_KILL_TIMEOUT_S)
            if reader.is_alive():
                log.error("ffmpeg mapper %d: its reader did not stop", self._proc.pid)
        try:
            self._proc.wait(_KILL_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg mapper %d did not exit", self._proc.pid)
        writer, self._writer = self._writer, None
        if writer is not None:
            try:
                writer.close()  # its socket is gone: only frees the muxer
            except Exception:
                pass

    # -- any thread -------------------------------------------------------------

    def kill(self) -> None:
        """Stop the subprocess, which wakes the reader and a write in
        progress; returns at once (a seek, a close). Idempotent. The decode
        thread's ``close`` waits for the reader, and owns the writer: a PyAV
        container is never touched from two threads."""
        if self._killed.is_set():
            return
        self._killed.set()
        try:
            self._proc.kill()
        except OSError:
            pass
        for sock in (self._in_conn, self._in_listener, self._out_listener):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)  # wakes a send or accept in progress
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        return not self._killed.is_set() and self._proc.poll() is None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def killed(self) -> bool:
        """Abandoned: what the decode thread was doing with it failed for that."""
        return self._killed.is_set()

    # -- reader and stderr threads ------------------------------------------------

    def _read(self) -> None:
        container = conn = None
        try:
            # ffmpeg connects with its first frame, once it has probed its
            # input; until then it is as idle as when it waits mid-stream.
            self._out_listener.settimeout(_IDLE_S)
            while True:
                try:
                    conn, _ = self._out_listener.accept()
                    break
                except socket.timeout:
                    if self._killed.is_set() or self._proc.poll() is not None:
                        raise self._failure("ffmpeg produced no output")
                    self._idle()
            conn.settimeout(_IDLE_S)
            container = av.open(_SocketReader(conn, self._idle), format="nut", buffer_size=_READ_CHUNK)
            stream = container.streams.video[0]
            scale = Fraction(stream.time_base.numerator, stream.time_base.denominator) / self._time_base
            for frame in container.decode(stream):
                if self._killed.is_set():
                    return
                if frame.pts is not None:
                    pts = round(frame.pts * scale) - _TS_OFFSET
                    # A packet without a PTS took its decode-clock tick.
                    frame.pts = pts if pts > -_DTS_LEAD // 2 else None
                frame.time_base = self._time_base
                self._delivering = True
                try:
                    self._on_frame(frame)
                    self.frames_out += 1
                    if frame.pts is not None:
                        self._release_through(frame.pts)
                finally:
                    self._progress_at = time.monotonic()
                    self._delivering = False
            if not self._finishing and not self._killed.is_set():
                raise self._failure("ffmpeg stopped mid-stream")
            self._release_through(None)
        except Exception as error:
            if not self._killed.is_set() and self.claim_failure():
                self._on_error(str(error) if isinstance(error, MapperError)
                               else str(self._failure(f"reading from ffmpeg failed: {error!r}")))
            # The decode thread's next write then fails at once, as a failure
            # already reported, rather than into a socket nobody reads.
            self.kill()
        finally:
            for owner in (container, conn):
                if owner is not None:
                    try:
                        owner.close()
                    except Exception:
                        pass

    def _idle(self) -> bool:
        """ffmpeg sent nothing for _IDLE_S (reader thread): if no packet went
        to it meanwhile either, it waits for one, and everything held goes
        on. False once killed, which ends the stream."""
        if self._killed.is_set():
            return False
        if time.monotonic() - self._written_at >= _IDLE_S:
            self._delivering = True
            try:
                self._release_through(None)
            finally:
                self._delivering = False
        return True

    def _release_through(self, pts: int | None) -> None:
        """Deliver held items whose frames are out (None: all of them);
        reader thread only."""
        while True:
            with self._lock:
                if pts is not None:
                    self._last_pts = pts if self._last_pts is None else max(self._last_pts, pts)
                if not self._held:
                    return
                after = self._held[0][0]
                if pts is not None and after is not None and after > self._last_pts:
                    return
                _after, item = self._held.popleft()
                self._released.notify_all()
            self._on_item(item)

    def _read_stderr(self) -> None:
        for line in iter(self._proc.stderr.readline, b""):
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr.append(text)
                log.debug("ffmpeg mapper: %s", text)

    def _failure(self, what: str) -> MapperError:
        said = " | ".join(self._stderr)
        return MapperError(f"display mapping: {what}" + (f"; ffmpeg said: {said}" if said else ""))
