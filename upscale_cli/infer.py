"""ONNX super-resolution inference stage.

A model is an .onnx file with a JSON manifest beside it
(model.onnx -> model.json):

    {
        "scale_factor": 2,
        "channel_order": "rgb",     // or "bgr"
        "value_range": [0.0, 1.0]   // input normalization target
    }

Models are expected to take NCHW float input with dynamic spatial dims and
produce NCHW float output scaled by scale_factor. If the manifest is missing,
one is generated with RGB [0, 1] defaults. Scale markers such as 3x or x4 are
read from the filename; filenames without one default to 2x.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

import av
import numpy as np

from .manifest import ModelManifest


def _add_nvidia_dll_dirs() -> None:
    """cuDNN 9 lazy-loads engine sub-DLLs by bare name; when CUDA/cuDNN come
    from nvidia pip wheels their bin dirs must be on PATH before onnxruntime
    loads. No-op when the wheels aren't installed (e.g. DirectML env)."""
    import os

    site = Path(np.__file__).resolve().parents[1]
    dirs = []
    nvidia = site / "nvidia"
    if nvidia.is_dir():
        dirs += [str(p) for p in nvidia.rglob("bin") if p.is_dir()]
    dirs += [str(p) for p in site.glob("tensorrt_libs") if p.is_dir()]
    if dirs:
        os.environ["PATH"] = ";".join(dirs) + ";" + os.environ["PATH"]
        for d in dirs:
            os.add_dll_directory(d)


_add_nvidia_dll_dirs()

import onnxruntime as ort  # noqa: E402

# EP preference: TensorRT -> CUDA -> DirectML -> CPU (per docs/PLAN.md).
_EP_ORDER = [
    ("tensorrt", "TensorrtExecutionProvider"),
    ("cuda", "CUDAExecutionProvider"),
    ("dml", "DmlExecutionProvider"),
    ("cpu", "CPUExecutionProvider"),
]
_EP_BY_ALIAS = dict(_EP_ORDER)


def _trt_options() -> dict:
    cache = Path("models") / ".trt_cache"
    cache.mkdir(parents=True, exist_ok=True)
    return {
        "trt_fp16_enable": True,
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": str(cache),
        "trt_timing_cache_enable": True,
        "trt_timing_cache_path": str(cache),
        # No auxiliary CUDA streams: with them enabled, output DMA can land
        # after run() returns and scribble freed host memory — observed as
        # native crashes in *other* threads (the libav decoder was the usual
        # victim) seconds into streaming.
        "trt_auxiliary_streams": "0",
    }


def _with_trt_profile(providers: list, input_name: str) -> list:
    """Attach an explicit dynamic-shape profile to the TensorRT provider:
    min 64x64 (tile/probe floor), opt 1080p, max 1440p input. One engine
    covers all shapes, so ORT's crash-prone rebuild-at-new-shape path never
    runs. Max is capped at 1440p to bound the engine's activation memory
    (TRT allocates for max); larger sources go through the tiling path."""
    out = []
    for p in providers:
        if isinstance(p, tuple) and p[0] == "TensorrtExecutionProvider":
            opts = dict(p[1])
            opts.update({
                "trt_profile_min_shapes": f"{input_name}:1x64x64x3",
                "trt_profile_opt_shapes": f"{input_name}:1x1080x1920x3",
                "trt_profile_max_shapes": f"{input_name}:1x1440x2560x3",
            })
            out.append((p[0], opts))
        else:
            out.append(p)
    return out


def resolve_providers(ep: str = "auto") -> list:
    """Provider list for InferenceSession; TensorRT entries carry options
    (fp16 engines + on-disk engine cache — first build per shape takes ~20 s,
    cached builds load in seconds)."""

    def entry(name: str):
        return (name, _trt_options()) if name == "TensorrtExecutionProvider" else name

    available = set(ort.get_available_providers())
    if ep != "auto":
        name = _EP_BY_ALIAS.get(ep)
        if name is None:
            raise ValueError(f"unknown EP '{ep}' (choose from {[a for a, _ in _EP_ORDER]})")
        if name not in available:
            raise RuntimeError(f"EP '{ep}' ({name}) not available; installed: {sorted(available)}")
        # Keep CPU as the final fallback for unsupported ops.
        return [entry(name)] + (["CPUExecutionProvider"] if name != "CPUExecutionProvider" else [])
    return [entry(name) for _, name in _EP_ORDER if name in available]


def _free_vram_mb() -> int | None:
    """Best-effort free-VRAM query (NVIDIA only); None if unknown."""
    import subprocess

    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return int(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


class OnnxUpscaler:
    """FrameStage: upscale frames through an ONNX model.

    tile_size: None = whole frame, int = fixed tile, "auto" = pick from free
    VRAM and fall back on out-of-memory errors. Tiles overlap by `overlap`
    pixels; each tile's outer overlap/2 margin is discarded on interior edges,
    so tiled output is identical to untiled as long as the model's receptive
    field is smaller than overlap/2.
    """

    _TILE_LADDER = [1024, 512, 256, 128]

    def __init__(self, model_path: str, ep: str = "auto",
                 tile_size: int | str | None = None, overlap: int = 16):
        if overlap % 2:
            raise ValueError("overlap must be even")
        self.tile_size = tile_size
        self.overlap = overlap
        self.model_path = model_path
        self.manifest = ModelManifest.load(model_path)
        providers = resolve_providers(ep)

        # Prefer a graph wrapped with GPU-side pre/post (uint8 NHWC IO) — the
        # CPU float passes it replaces were the pipeline's largest cost.
        self.uint8_io = False
        try:
            import onnx

            from .wrap_uint8 import wrap_uint8_io

            wrapped = wrap_uint8_io(
                onnx.load(model_path), self.manifest.value_range, self.manifest.channel_order
            )
            # Explicit TRT optimization profile: one engine covers every input
            # size. Without it, a new shape mid-stream triggers ORT's engine
            # rebuild path — observed to die with a native access violation
            # when the first real frame's shape wasn't in the cache.
            wrapped_providers = _with_trt_profile(providers, "u8wrap_in")
            self.session = ort.InferenceSession(
                wrapped.SerializeToString(), providers=wrapped_providers
            )
            # Inputs beyond the TRT profile max must go through tiling, or
            # ORT falls back to the rebuild-at-new-shape path.
            self._profile_max_hw = (
                (1440, 2560)
                if any(isinstance(p, tuple) and p[0] == "TensorrtExecutionProvider"
                       for p in wrapped_providers)
                else None
            )
            # Some graphs load but fail at run time on a given EP — probe now
            # (at the profile's min shape) so we can fall back to numpy.
            probe = np.zeros((1, 64, 64, 3), dtype=np.uint8)
            self.session.run(None, {self.session.get_inputs()[0].name: probe})
            self.uint8_io = True
        except Exception as err:
            print(f"model: uint8 wrap unavailable ({err}); using numpy pre/post", file=sys.stderr)
            self.session = ort.InferenceSession(model_path, providers=providers)
        self.active_provider = self.session.get_providers()[0]

        inp = self.session.get_inputs()[0]
        out = self.session.get_outputs()[0]
        self.input_name = inp.name
        self.output_name = out.name
        self.fp16 = inp.type == "tensor(float16)"
        self._dtype = np.float16 if self.fp16 else np.float32
        self.scale_factor = self.manifest.scale_factor  # may be None until first frame

        print(
            f"model: {Path(model_path).name} | provider: {self.active_provider}"
            f" | io: {'uint8-wrapped' if self.uint8_io else ('fp16' if self.fp16 else 'fp32')}",
            file=sys.stderr,
        )

    # -- array-level API (reused by the tiling stage later) -----------------

    def infer_array(self, rgb: np.ndarray) -> np.ndarray:
        """uint8 HWC RGB in -> uint8 HWC RGB out (scaled).

        Perf note: transposes happen on uint8 arrays and all float math runs
        in-place on contiguous buffers — element-wise ops on strided fp32
        views were the single slowest stage of the whole pipeline. With a
        uint8-wrapped graph none of that runs at all: the session does it all
        on the execution provider.
        """
        if self.uint8_io:
            y = self.session.run(
                [self.output_name], {self.input_name: np.ascontiguousarray(rgb)[None]}
            )[0]
            return y[0]  # uint8 HWC straight from the graph

        lo, hi = self.manifest.value_range
        chw = rgb.transpose(2, 0, 1)  # HWC -> CHW view
        if self.manifest.channel_order == "bgr":
            chw = chw[::-1]
        x = np.ascontiguousarray(chw).astype(self._dtype)  # uint8 copy, then SIMD convert
        x *= np.asarray((hi - lo) / 255.0, dtype=self._dtype)
        if lo:
            x += np.asarray(lo, dtype=self._dtype)

        y = self.session.run([self.output_name], {self.input_name: x[None]})[0]

        z = np.ascontiguousarray(y[0]).astype(np.float32, copy=False)
        z *= 255.0 / (hi - lo)
        if lo:
            z -= lo * 255.0 / (hi - lo)
        np.rint(z, out=z)
        np.clip(z, 0, 255, out=z)
        u8 = z.astype(np.uint8)  # CHW uint8
        if self.manifest.channel_order == "bgr":
            u8 = u8[::-1]
        return np.ascontiguousarray(u8.transpose(1, 2, 0))  # -> HWC

    def _tile_starts(self, dim: int, tile: int) -> list[int]:
        if dim <= tile:
            return [0]
        step = tile - self.overlap
        starts = list(range(0, dim - tile, step))
        starts.append(dim - tile)  # final tile flush with the edge
        return starts

    def infer_array_tiled(self, rgb: np.ndarray, tile: int) -> np.ndarray:
        h, w = rgb.shape[:2]
        if h <= tile and w <= tile:
            return self.infer_array(rgb)
        m = self.overlap // 2
        out: np.ndarray | None = None
        s = self.scale_factor
        for y0 in self._tile_starts(h, tile):
            for x0 in self._tile_starts(w, tile):
                th, tw = min(tile, h - y0), min(tile, w - x0)
                tile_out = self.infer_array(rgb[y0 : y0 + th, x0 : x0 + tw])
                if s is None:
                    s = round(tile_out.shape[0] / th)
                    self.scale_factor = s
                if out is None:
                    out = np.empty((h * s, w * s, 3), dtype=np.uint8)
                # Discard overlap margins on interior edges.
                top = m if y0 > 0 else 0
                left = m if x0 > 0 else 0
                bottom = th - (m if y0 + th < h else 0)
                right = tw - (m if x0 + tw < w else 0)
                out[(y0 + top) * s : (y0 + bottom) * s, (x0 + left) * s : (x0 + right) * s] = \
                    tile_out[top * s : bottom * s, left * s : right * s]
        return out

    def _pick_auto_tile(self, h: int, w: int) -> int | None:
        """Initial tile size for 'auto' mode; None means try the whole frame."""
        free_mb = _free_vram_mb()
        if free_mb is None:
            return None  # unknown; optimistic, OOM ladder catches it
        scale = self.scale_factor or 4  # assume worst common case pre-inference
        # Rough activation footprint: (in + out) pixels * 3ch * 4B * fudge for
        # intermediate layers.
        fudge = 24
        bytes_per_px = 3 * 4 * (1 + scale * scale) * fudge
        budget = free_mb * 1024 * 1024 * 0.8
        if h * w * bytes_per_px <= budget:
            return None
        for tile in self._TILE_LADDER:
            if tile * tile * bytes_per_px <= budget:
                return tile
        return self._TILE_LADDER[-1]

    def _infer_with_fallback(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        max_hw = getattr(self, "_profile_max_hw", None)
        if max_hw is not None and (h > max_hw[0] or w > max_hw[1]):
            # Beyond the TRT optimization profile: tile instead of letting
            # ORT rebuild the engine mid-stream.
            return self.infer_array_tiled(rgb, 1024)
        if isinstance(self.tile_size, int):
            return self.infer_array_tiled(rgb, self.tile_size)
        if self.tile_size is None:
            return self.infer_array(rgb)
        # auto: pick a starting point, walk down the ladder on OOM
        attempts: list[int | None] = [self._pick_auto_tile(h, w)]
        attempts += [t for t in self._TILE_LADDER if attempts[0] is None or t < attempts[0]]
        last_err: Exception | None = None
        for tile in attempts:
            try:
                result = self.infer_array(rgb) if tile is None else self.infer_array_tiled(rgb, tile)
                if tile is not None and self.tile_size == "auto":
                    print(f"model: auto tile size {tile}", file=sys.stderr)
                self.tile_size = tile if tile is not None else None  # lock in for later frames
                return result
            except Exception as err:  # ORT raises RuntimeException on OOM
                if "memory" not in str(err).lower() and "alloc" not in str(err).lower():
                    raise
                last_err = err
        raise RuntimeError(f"out of memory even at tile size {self._TILE_LADDER[-1]}") from last_err

    # -- FrameStage API ------------------------------------------------------

    def process(self, frame: av.VideoFrame) -> Iterable[av.VideoFrame]:
        rgb = frame.to_ndarray(format="rgb24")
        up = self._infer_with_fallback(rgb)
        if self.scale_factor is None:
            self.scale_factor = round(up.shape[0] / rgb.shape[0])
            print(f"model: inferred scale factor x{self.scale_factor}", file=sys.stderr)

        out = av.VideoFrame.from_ndarray(up, format="rgb24")
        out.pts = frame.pts
        out.time_base = frame.time_base
        yield out

    def flush(self) -> Iterable[av.VideoFrame]:
        return ()
