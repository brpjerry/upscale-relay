"""Pipeline building blocks: FrameSource -> [FrameStage...] -> FrameSink.

Frames flow through as av.VideoFrame objects carrying their original PTS in the
source stream's time_base. Stages must preserve PTS; they may change frame
dimensions and pixel format.
"""

from __future__ import annotations

import sys
from fractions import Fraction
from typing import Iterable, Iterator, Protocol

import av
from av.codec.hwaccel import HWAccel

# Pixel formats that indicate a frame still lives in GPU memory.
_HW_PIX_FMTS = {"cuda", "d3d11", "d3d11va_vld", "dxva2_vld", "vaapi", "qsv", "videotoolbox"}

# hwaccel device types to try for "auto", in order.
_AUTO_HW_DEVICES = ["cuda", "d3d11va"]


class FrameStage(Protocol):
    def process(self, frame: av.VideoFrame) -> Iterable[av.VideoFrame]: ...

    def flush(self) -> Iterable[av.VideoFrame]:
        return ()


class FrameSource:
    """Decodes the first video stream of a file, yielding CPU frames with PTS.

    hwaccel: "auto" (try NVDEC/D3D11VA, fall back to software), a specific
    device type ("cuda", "d3d11va"), or "none".
    """

    def __init__(self, path: str, hwaccel: str = "auto"):
        self.path = path
        self.hwaccel_requested = hwaccel
        self.hwaccel_active: str | None = None
        self._container, self._stream = self._open(hwaccel)

    def _open(self, hwaccel: str):
        if hwaccel != "none":
            devices = _AUTO_HW_DEVICES if hwaccel == "auto" else [hwaccel]
            for device in devices:
                try:
                    container = av.open(
                        self.path,
                        hwaccel=HWAccel(device_type=device, allow_software_fallback=False),
                    )
                    stream = container.streams.video[0]
                    # Probe: decode one frame and make sure we can get it to CPU.
                    probe = av.open(
                        self.path,
                        hwaccel=HWAccel(device_type=device, allow_software_fallback=False),
                    )
                    pstream = probe.streams.video[0]
                    for frame in probe.decode(pstream):
                        _to_cpu(frame)
                        break
                    probe.close()
                    self.hwaccel_active = device
                    return container, stream
                except Exception:
                    if hwaccel != "auto":
                        print(
                            f"warning: hwaccel '{device}' unavailable, using software decode",
                            file=sys.stderr,
                        )
        container = av.open(self.path)
        return container, container.streams.video[0]

    @property
    def stream(self) -> av.VideoStream:
        return self._stream

    @property
    def time_base(self) -> Fraction:
        return self._stream.time_base

    @property
    def average_rate(self) -> Fraction | None:
        return self._stream.average_rate

    def __iter__(self) -> Iterator[av.VideoFrame]:
        for frame in self._container.decode(self._stream):
            yield _to_cpu(frame)

    def close(self) -> None:
        self._container.close()

    def __enter__(self) -> "FrameSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _to_cpu(frame: av.VideoFrame) -> av.VideoFrame:
    """Transfer a hardware frame to system memory (no-op for software frames)."""
    if frame.format.name in _HW_PIX_FMTS:
        cpu = frame.reformat(format="nv12")
        cpu.pts = frame.pts
        cpu.time_base = frame.time_base
        return cpu
    return frame


class FrameSink:
    """Encodes frames to a file, preserving PTS and the source time_base.

    The output stream is created lazily from the first frame, so upstream
    stages may change dimensions before anything is locked in.
    """

    def __init__(
        self,
        path: str,
        time_base: Fraction,
        rate: Fraction | None = None,
        codec: str = "libx264",
        pix_fmt: str = "yuv420p",
        options: dict[str, str] | None = None,
    ):
        self.path = path
        self._time_base = time_base
        self._rate = rate
        self._codec = codec
        self._pix_fmt = pix_fmt
        self._options = options if options is not None else {"crf": "12", "preset": "medium"}
        self._container = av.open(path, mode="w")
        self._stream: av.VideoStream | None = None
        self.frames_written = 0
        self.pts_written: list[float] = []  # seconds, for verification

    def _init_stream(self, frame: av.VideoFrame) -> av.VideoStream:
        # Note: the muxer picks the output stream time_base (e.g. 1/1000 for MKV);
        # PyAV rescales packets from each frame's own time_base.
        stream = self._container.add_stream(self._codec, rate=self._rate, options=self._options)
        stream.width = frame.width
        stream.height = frame.height
        stream.pix_fmt = self._pix_fmt
        return stream

    def write(self, frame: av.VideoFrame) -> None:
        if self._stream is None:
            self._stream = self._init_stream(frame)
        if frame.format.name != self._pix_fmt:
            converted = frame.reformat(format=self._pix_fmt)
            converted.pts = frame.pts
            converted.time_base = frame.time_base
            frame = converted
        if frame.time_base is None:
            frame.time_base = self._time_base
        if frame.pts is not None:
            self.pts_written.append(float(frame.pts * frame.time_base))
        for packet in self._stream.encode(frame):
            self._container.mux(packet)
        self.frames_written += 1

    def close(self) -> None:
        if self._stream is not None:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        self._container.close()

    def __enter__(self) -> "FrameSink":
        return self

    def __exit__(self, exc_type, *exc) -> None:
        if exc_type is None:
            self.close()
        else:
            self._container.close()


def run_pipeline(
    source: FrameSource,
    sink: FrameSink,
    stages: list[FrameStage] | None = None,
    progress_every: int = 100,
) -> int:
    """Pump frames source -> stages -> sink. Returns frames decoded."""
    import time

    stages = stages or []
    decoded = 0
    start = time.perf_counter()

    def emit(frames: Iterable[av.VideoFrame], stage_idx: int) -> None:
        for frame in frames:
            if stage_idx < len(stages):
                emit(stages[stage_idx].process(frame), stage_idx + 1)
            else:
                sink.write(frame)

    for frame in source:
        decoded += 1
        emit([frame], 0)
        if progress_every and decoded % progress_every == 0:
            fps = decoded / (time.perf_counter() - start)
            print(f"\r{decoded} frames  ({fps:.1f} fps)", end="", file=sys.stderr, flush=True)

    # Flush stages in order, feeding tail output through the rest of the chain.
    for i, stage in enumerate(stages):
        emit(stage.flush(), i + 1)

    if progress_every:
        elapsed = time.perf_counter() - start
        fps = decoded / elapsed if elapsed > 0 else 0.0
        print(
            f"\r{decoded} frames decoded, {sink.frames_written} written "
            f"in {elapsed:.1f}s ({fps:.1f} fps)",
            file=sys.stderr,
        )
    return decoded
