"""Synthetic test clip generator (no external media needed)."""

from __future__ import annotations

from fractions import Fraction

import av
import numpy as np


def make_sample(path: str, frames: int = 240, width: int = 640, height: int = 360, fps: int = 30) -> None:
    """Moving gradient + bouncing box: enough texture to exercise codecs."""
    container = av.open(path, mode="w")
    stream = container.add_stream("libx264", rate=Fraction(fps, 1), options={"crf": "18", "preset": "fast"})
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    time_base = Fraction(1, fps)

    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    box = max(16, height // 8)
    for i in range(frames):
        img = np.empty((height, width, 3), dtype=np.uint8)
        img[..., 0] = ((xx / width * 255) + i * 2) % 256
        img[..., 1] = ((yy / height * 255) + i) % 256
        img[..., 2] = ((xx + yy) / (width + height) * 255) % 256
        bx = int((np.sin(i / 20) * 0.5 + 0.5) * (width - box))
        by = int((np.cos(i / 15) * 0.5 + 0.5) * (height - box))
        img[by : by + box, bx : bx + box] = (255, 255, 255)

        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        frame.time_base = time_base
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    print(f"wrote {frames} frames @ {fps} fps, {width}x{height} -> {path}")
