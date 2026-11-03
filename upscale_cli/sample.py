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
    reformatter = av.video.reformatter.VideoReformatter()

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

        frame = reformatter.reformat(av.VideoFrame.from_ndarray(img, format="rgb24"), format="yuv420p")
        frame.pts = i
        frame.time_base = time_base
        for packet in stream.encode(frame):
            container.mux(packet)

    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    print(f"wrote {frames} frames @ {fps} fps, {width}x{height} -> {path}")


# Grey patches of the HDR clip, in nits: SDR white (203) among dimmer and
# brighter ones, up to the clip's mastering peak.
HDR_PATCH_NITS = (50, 100, 203, 400, 600, 1000)


def pq_code(nits: float) -> int:
    """10-bit limited-range PQ code value of a grey at ``nits``."""
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    p = (max(0.0, nits) / 10000) ** m1
    return round(64 + 876 * ((c1 + c2 * p) / (1 + c3 * p)) ** m2)


def make_hdr_sample(path: str, frames: int = 240, width: int = 640, height: int = 360, fps: int = 30) -> None:
    """10-bit HDR10 (PQ, BT.2020) HEVC: a ramp to 1000 nits over the top
    half, grey patches at HDR_PATCH_NITS below it, and a 1000-nit box that
    moves. Shown as HDR, the brightest patches stand out from SDR white;
    tone-mapped to SDR they all but merge."""
    container = av.open(path, mode="w")
    stream = container.add_stream("libx265", rate=Fraction(fps, 1), options={
        "preset": "ultrafast",
        "x265-params": (
            "hdr10=1:repeat-headers=1:max-cll=1000,400:colorprim=bt2020:transfer=smpte2084:"
            "colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
            "WP(15635,16450)L(10000000,50)"
        ),
    })
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p10le"
    cc = stream.codec_context
    # BT.2020 non-constant luminance, limited range, BT.2020, SMPTE ST 2084.
    cc.colorspace, cc.color_range, cc.color_primaries, cc.color_trc = 9, 1, 9, 16
    time_base = Fraction(1, fps)

    top = height // 2
    luma_base = np.empty((height, width), np.uint16)
    ramp = np.array([pq_code(1000 * x / max(1, width - 1)) for x in range(width)], np.uint16)
    luma_base[:top] = ramp
    luma_base[top:] = pq_code(5)
    gap = max(2, width // 64)
    patch = (width - gap * (len(HDR_PATCH_NITS) + 1)) // len(HDR_PATCH_NITS)
    for index, nits in enumerate(HDR_PATCH_NITS):
        left = gap + index * (patch + gap)
        luma_base[top + gap:height - gap, left:left + patch] = pq_code(nits)
    chroma = np.full((height // 2, width // 2), 512, np.uint16)  # grey
    box = max(16, height // 10)
    bright = pq_code(1000)
    for i in range(frames):
        luma = luma_base.copy()
        bx = int((np.sin(i / 20) * 0.5 + 0.5) * (width - box))
        luma[top - box // 2:top + box // 2, bx:bx + box] = bright
        frame = av.VideoFrame(width, height, "yuv420p10le")
        for plane, values in zip(frame.planes, (luma, chroma, chroma)):
            rows = np.zeros((plane.height, plane.line_size // 2), np.uint16)
            rows[:, :values.shape[1]] = values
            plane.update(rows.tobytes())
        frame.pts = i
        frame.time_base = time_base
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    print(f"wrote {frames} HDR10 frames @ {fps} fps, {width}x{height} -> {path}")
