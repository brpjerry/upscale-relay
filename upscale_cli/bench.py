"""Benchmark harness: which models are real-time viable on this machine?

For every .onnx model in a directory, measures at several input resolutions:
decode fps, inference fps (untiled and tiled), encode fps per quality tier at
the model's output size, and end-to-end pipeline fps — plus GPU utilization /
VRAM peak (NVIDIA only). Emits a markdown report.
"""

from __future__ import annotations

import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from .encode import TIERS, select_encoder
from .fit import fit_dimensions
from .infer import OnnxUpscaler

SIZES = {"720p": (1280, 720), "1080p": (1920, 1080), "1440p": (2560, 1440)}


class GpuSampler:
    """Polls nvidia-smi in the background; records peak utilization and VRAM."""

    def __init__(self, interval: float = 0.25):
        self.interval = interval
        self.max_util = 0
        self.max_mem = 0
        self.available = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5,
                )
                if out.returncode != 0:
                    self.available = False
                    return
                util, mem = out.stdout.strip().splitlines()[0].split(",")
                self.max_util = max(self.max_util, int(util))
                self.max_mem = max(self.max_mem, int(mem))
            except Exception:
                self.available = False
                return
            self._stop.wait(self.interval)

    def __enter__(self) -> "GpuSampler":
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def _synthetic_frames(width: int, height: int, count: int) -> list[np.ndarray]:
    """Deterministic textured frames (in RAM) for feeding stages directly."""
    rng = np.random.default_rng(7)
    base = rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)
    frames = []
    for i in range(count):
        frames.append(np.roll(base, shift=i * 4, axis=1))
    return frames


def _bench_decode(width: int, height: int, frames: int, workdir: Path) -> float:
    """Encode a clip once, then measure pure decode fps."""
    clip = workdir / f"bench_{width}x{height}.mkv"
    if not clip.exists():
        container = av.open(str(clip), mode="w")
        stream = container.add_stream("libx264", rate=Fraction(30, 1),
                                      options={"crf": "18", "preset": "fast"})
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for i, rgb in enumerate(_synthetic_frames(width, height, frames)):
            f = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format="yuv420p")
            f.pts, f.time_base = i, Fraction(1, 30)
            for pkt in stream.encode(f):
                container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
        container.close()

    from .stages import FrameSource

    start = time.perf_counter()
    n = 0
    with FrameSource(str(clip)) as src:
        for _ in src:
            n += 1
    return n / (time.perf_counter() - start)


def _bench_inference(up: OnnxUpscaler, frames: list[np.ndarray], tile: int | None) -> float | str:
    try:
        run = (lambda f: up.infer_array_tiled(f, tile)) if tile else up.infer_array
        run(frames[0])  # warmup / shape lock-in
        start = time.perf_counter()
        for f in frames:
            run(f)
        return len(frames) / (time.perf_counter() - start)
    except Exception as err:
        return f"error: {str(err).splitlines()[0][:60]}"


def _bench_encode(width: int, height: int, frames: list[np.ndarray], tier: str, workdir: Path) -> float | str:
    try:
        codec, pix_fmt, options = select_encoder(tier)
    except RuntimeError:
        return "unavailable"
    out = av.open(str(workdir / f"enc_{tier}.mkv"), mode="w")
    stream = out.add_stream(codec, rate=Fraction(30, 1), options=options)
    stream.width, stream.height, stream.pix_fmt = width, height, pix_fmt
    start = time.perf_counter()
    for i, rgb in enumerate(frames):
        f = av.VideoFrame.from_ndarray(rgb, format="rgb24").reformat(format=pix_fmt)
        f.pts, f.time_base = i, Fraction(1, 30)
        for pkt in stream.encode(f):
            out.mux(pkt)
    for pkt in stream.encode(None):
        out.mux(pkt)
    out.close()
    return len(frames) / (time.perf_counter() - start)


def _fmt(v: float | str) -> str:
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def run_bench(models_dir: str, out_path: str, frames: int = 48, ep: str = "auto",
              fit: tuple[int, int] | None = None) -> None:
    import sys
    import tempfile

    models = sorted(Path(models_dir).glob("*.onnx"))
    if not models:
        raise SystemExit(f"no .onnx models in {models_dir}")

    workdir = Path(tempfile.mkdtemp(prefix="upscale-bench-"))
    lines = [
        "# upscale-cli benchmark",
        "",
        f"- frames per measurement: {frames}",
        f"- execution provider request: {ep}",
        "",
    ]

    # Decode baseline is model-independent.
    lines += ["## Decode (NVDEC/software auto)", "", "| input | fps |", "|---|---|"]
    for name, (w, h) in SIZES.items():
        fps = _bench_decode(w, h, max(frames, 60), workdir)
        lines.append(f"| {name} | {fps:.1f} |")
        print(f"decode {name}: {fps:.1f} fps", file=sys.stderr)
    lines.append("")

    for model_path in models:
        lines += [f"## Model: {model_path.name}", ""]
        header = "| input | untiled fps | tiled-512 fps | " + " | ".join(
            f"{t} enc fps" for t in TIERS
        ) + " | e2e fps (vl) | GPU util peak | VRAM peak MB |"
        sep = "|" + "---|" * (len(TIERS) + 6)
        lines += [header, sep]

        for size_name, (w, h) in SIZES.items():
            up = OnnxUpscaler(str(model_path), ep=ep)
            src_frames = _synthetic_frames(w, h, frames)

            with GpuSampler() as gpu:
                untiled = _bench_inference(up, src_frames, tile=None)
                tiled = _bench_inference(up, src_frames, tile=512)

                scale = up.scale_factor or 2
                ow, oh = w * scale, h * scale
                if fit:
                    ow, oh = fit_dimensions(ow, oh, *fit)
                out_frames = _synthetic_frames(ow, oh, min(frames, 24))
                enc_results = [_bench_encode(ow, oh, out_frames, tier, workdir) for tier in TIERS]

                # End-to-end (low-bandwidth HEVC): inference + scale + encode serially.
                e2e: float | str
                if isinstance(untiled, str):
                    e2e = "n/a"
                else:
                    codec, pix_fmt, options = select_encoder("hevc-qp18")
                    out = av.open(str(workdir / "e2e.mkv"), mode="w")
                    stream = out.add_stream(codec, rate=Fraction(30, 1), options=options)
                    start = time.perf_counter()
                    for i, rgb in enumerate(src_frames[: min(frames, 24)]):
                        upres = up.infer_array(rgb)
                        f = av.VideoFrame.from_ndarray(upres, format="rgb24")
                        if fit:
                            fw, fh = fit_dimensions(f.width, f.height, *fit)
                            f = f.reformat(width=fw, height=fh, interpolation="LANCZOS")
                        if stream.width == 0:
                            stream.width, stream.height, stream.pix_fmt = f.width, f.height, pix_fmt
                        f = f.reformat(format=pix_fmt)
                        f.pts, f.time_base = i, Fraction(1, 30)
                        for pkt in stream.encode(f):
                            out.mux(pkt)
                    for pkt in stream.encode(None):
                        out.mux(pkt)
                    out.close()
                    e2e = min(frames, 24) / (time.perf_counter() - start)

            gpu_util = f"{gpu.max_util}%" if gpu.available else "n/a"
            gpu_mem = str(gpu.max_mem) if gpu.available else "n/a"
            cells = [size_name, _fmt(untiled), _fmt(tiled), *[_fmt(r) for r in enc_results],
                     _fmt(e2e), gpu_util, gpu_mem]
            lines.append("| " + " | ".join(cells) + " |")
            print(f"{model_path.name} @ {size_name}: untiled {_fmt(untiled)}, "
                  f"tiled {_fmt(tiled)}, e2e {_fmt(e2e)} fps", file=sys.stderr)
        lines.append("")

    lines += [
        "Real-time viability: e2e fps must sustain the content frame rate "
        "(24/30/60). Prefer the fastest model that clears it with ~30% headroom.",
        "",
    ]
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"report written to {out_path}")
