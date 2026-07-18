"""Post-inference resolution fitter.

Resamples frames to fit inside a target display resolution, preserving aspect
ratio: Lanczos downscale when the model overshoots, mild upscale when it falls
short. Final dimensions are rounded to a multiple of `align` (default 2, as
required by yuv420p; pass 16 for strict encoder alignment). Rounding keeps the
aspect ratio within align/2 pixels of exact, which avoids pad+crop-metadata
gymnastics that many players ignore.

CPU swscale for now. In the streaming server, frames should stay on the GPU
(scale_cuda / zscale) — this stage defines the semantics, not the final home.
"""

from __future__ import annotations

import sys
from typing import Iterable

import av


DEFAULT_RESIZE_ALGORITHM = "lanczos"
RESIZE_ALGORITHMS = (
    "fast-bilinear",
    "bilinear",
    "bicubic",
    "area",
    "bicublin",
    "gaussian",
    "sinc",
    "lanczos",
    "spline",
)
_INTERPOLATION_NAMES = {
    "fast-bilinear": "FAST_BILINEAR",
    "bilinear": "BILINEAR",
    "bicubic": "BICUBIC",
    "area": "AREA",
    "bicublin": "BICUBLIN",
    "gaussian": "GAUSS",
    "sinc": "SINC",
    "lanczos": "LANCZOS",
    "spline": "SPLINE",
}


def interpolation_for_algorithm(algorithm: str) -> str:
    """Translate a public resize-algorithm name to PyAV/swscale's name."""
    try:
        return _INTERPOLATION_NAMES[algorithm]
    except KeyError as err:
        choices = ", ".join(RESIZE_ALGORITHMS)
        raise ValueError(f"unknown resize algorithm {algorithm!r}; choose from {choices}") from err


def aligned_target_dimensions(target_w: int, target_h: int, align: int = 2) -> tuple[int, int]:
    """Target dimensions rounded down for pixel-format/encoder alignment."""
    if target_w <= 0 or target_h <= 0 or align <= 0:
        raise ValueError("target dimensions and alignment must be positive")
    return max(align, target_w // align * align), max(align, target_h // align * align)


def cover_crop_box(
    w: int, h: int, target_w: int, target_h: int, align: int = 2,
) -> tuple[int, int, int, int]:
    """Centered, aligned source rectangle with the target aspect ratio.

    The returned ``(x, y, width, height)`` is suitable for cropping before a
    single resize to :func:`aligned_target_dimensions`. This avoids encoding
    the off-screen overflow that client-side cover/panscan used to require.
    """
    if w <= 0 or h <= 0:
        raise ValueError("source dimensions must be positive")
    tw, th = aligned_target_dimensions(target_w, target_h, align)
    aw = max(align, w // align * align)
    ah = max(align, h // align * align)
    if aw * th > ah * tw:
        crop_h = ah
        crop_w = max(align, int(crop_h * tw / th) // align * align)
    else:
        crop_w = aw
        crop_h = max(align, int(crop_w * th / tw) // align * align)
    crop_w = min(crop_w, aw)
    crop_h = min(crop_h, ah)
    x = max(0, ((w - crop_w) // (2 * align)) * align)
    y = max(0, ((h - crop_h) // (2 * align)) * align)
    return x, y, crop_w, crop_h


def fit_dimensions(w: int, h: int, target_w: int, target_h: int, align: int = 2) -> tuple[int, int]:
    """Largest align-rounded (w, h) with the same aspect that fits inside target."""
    scale = min(target_w / w, target_h / h)
    fw = max(align, round(w * scale / align) * align)
    fh = max(align, round(h * scale / align) * align)
    # Rounding up may poke past the target; step back one unit if so.
    if fw > target_w:
        fw -= align
    if fh > target_h:
        fh -= align
    return fw, fh


def cover_dimensions(w: int, h: int, target_w: int, target_h: int, align: int = 2) -> tuple[int, int]:
    """Legacy helper: smallest same-aspect dimensions that cover a target.

    The inverse of `fit_dimensions`: instead of the largest same-aspect box that
    fits inside the target, the smallest one that fully contains it (one axis is
    exact, the other overflows). The streaming server now uses
    :func:`cover_crop_box` instead so it does not transmit this overflow.
    Rounding never dips below the target."""
    scale = max(target_w / w, target_h / h)
    fw = max(align, round(w * scale / align) * align)
    fh = max(align, round(h * scale / align) * align)
    # Rounding down mustn't create a gap on the covering axis; step up if so.
    if fw < target_w:
        fw += align
    if fh < target_h:
        fh += align
    return fw, fh


class FitStage:
    """FrameStage: fit frames inside target_w x target_h."""

    def __init__(self, target_w: int, target_h: int, align: int = 2,
                 resize_algorithm: str = DEFAULT_RESIZE_ALGORITHM):
        self.target_w = target_w
        self.target_h = target_h
        self.align = align
        self.interpolation = interpolation_for_algorithm(resize_algorithm)
        self._out_dims: tuple[int, int] | None = None

    def process(self, frame: av.VideoFrame) -> Iterable[av.VideoFrame]:
        if self._out_dims is None:
            self._out_dims = fit_dimensions(
                frame.width, frame.height, self.target_w, self.target_h, self.align
            )
            direction = "downscale" if self._out_dims[0] < frame.width else (
                "upscale" if self._out_dims[0] > frame.width else "passthrough"
            )
            print(
                f"fit: {frame.width}x{frame.height} -> {self._out_dims[0]}x{self._out_dims[1]}"
                f" ({direction}, target {self.target_w}x{self.target_h})",
                file=sys.stderr,
            )
        ow, oh = self._out_dims
        if (frame.width, frame.height) == (ow, oh):
            yield frame
            return
        scaled = frame.reformat(width=ow, height=oh, interpolation=self.interpolation)
        scaled.pts = frame.pts
        scaled.time_base = frame.time_base
        yield scaled

    def flush(self) -> Iterable[av.VideoFrame]:
        return ()
