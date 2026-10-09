"""Uplink sample aspect ratio read from codec parameter sets (#7 fallback).

A client that sends no open_session.video.sample_aspect_ratio (Android cannot
see an aspect declared only in the bitstream of a Matroska file) still gets
its anamorphic source fitted correctly.
"""

from __future__ import annotations

import asyncio
import json
from fractions import Fraction
from pathlib import Path

import aiohttp
import av
import numpy as np
import pytest

from ports import free_port_pair
from relay_media import VideoTrack
from relay_server.server import RelayServer
from relay_server.source_aspect import _rbsp, sample_aspect_ratio_from_parameter_sets

ROOT = Path(__file__).resolve().parents[1]


class _Writer:
    def __init__(self):
        self.bits: list[int] = []

    def u(self, count, value):
        self.bits += [(value >> (count - 1 - i)) & 1 for i in range(count)]

    def ue(self, value):
        coded = value + 1
        self.u(coded.bit_length() - 1, 0)
        self.u(coded.bit_length(), coded)

    def se(self, value):
        self.ue(2 * value - 1 if value > 0 else -2 * value)

    def rbsp(self) -> bytes:
        bits = self.bits + [1]  # rbsp_stop_one_bit
        bits += [0] * (-len(bits) % 8)
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))


def _escape(rbsp: bytes) -> bytes:
    out, zeros = bytearray(), 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _sps(profile=100, *, chroma=1, scaling=False, poc_type=0, interlaced=False,
         crop=False, vui=True, aspect=True, idc=255, sar=(16, 15)) -> bytes:
    w = _Writer()
    w.u(8, profile)
    w.u(8, 0)
    w.u(8, 40)
    w.ue(0)
    if profile in (100, 110, 122, 244):
        w.ue(chroma)
        if chroma == 3:
            w.u(1, 0)
        w.ue(0)
        w.ue(0)
        w.u(1, 0)
        w.u(1, int(scaling))
        if scaling:
            for index in range(8 if chroma != 3 else 12):
                present = index in (0, 6)
                w.u(1, int(present))
                if present:
                    for _ in range(16 if index < 6 else 64):
                        w.se(1)  # an explicit, non-default list
    w.ue(4)
    w.ue(poc_type)
    if poc_type == 0:
        w.ue(2)
    elif poc_type == 1:
        w.u(1, 0)
        w.se(-3)
        w.se(2)
        w.ue(3)
        for offset in (1, -1, 5):
            w.se(offset)
    w.ue(4)
    w.u(1, 0)
    w.ue(44)  # 720 px
    w.ue(35)  # 576 px
    w.u(1, int(not interlaced))
    if interlaced:
        w.u(1, 1)
    w.u(1, 1)
    w.u(1, int(crop))
    if crop:
        for value in (0, 4, 0, 8):
            w.ue(value)
    w.u(1, int(vui))
    if vui:
        w.u(1, int(aspect))
        if aspect:
            w.u(8, idc)
            if idc == 255:
                w.u(16, sar[0])
                w.u(16, sar[1])
        w.u(1, 0)  # overscan_info_present_flag; the rest is never read
    return b"\x67" + _escape(w.rbsp())


def _avcc(sps: bytes) -> bytes:
    pps = b"\x68\xee\x3c\x80"
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1]) + len(sps).to_bytes(2, "big") + sps
            + b"\x01" + len(pps).to_bytes(2, "big") + pps)


def _read(extradata: bytes):
    return sample_aspect_ratio_from_parameter_sets("h264", extradata, 720, 576)


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({}, Fraction(16, 15)),
    ({"profile": 66}, Fraction(16, 15)),                     # baseline: no chroma fields
    ({"chroma": 3, "scaling": True}, Fraction(16, 15)),      # 4:4:4 with 12 scaling lists
    ({"chroma": 2, "scaling": True}, Fraction(16, 15)),
    ({"poc_type": 1}, Fraction(16, 15)),
    ({"poc_type": 2, "interlaced": True, "crop": True}, Fraction(16, 15)),
    ({"idc": 2}, Fraction(12, 11)),                          # Table E-1 entry
    ({"idc": 14}, Fraction(4, 3)),
    ({"sar": (256, 3)}, Fraction(256, 3)),
    ({"idc": 0}, None),                                      # unspecified
    ({"idc": 200}, None),                                    # reserved
    ({"sar": (16, 0)}, None),
    ({"aspect": False}, None),
    ({"vui": False}, None),
])
def test_h264_sps_aspect(kwargs, expected):
    sps = _sps(**kwargs)
    assert _read(_avcc(sps)) == expected
    assert _read(b"\x00\x00\x00\x01" + sps + b"\x00\x00\x01\x68\xee\x3c\x80") == expected  # Annex B


def test_emulation_prevention_is_removed():
    assert _rbsp(b"\x01\x00\x00\x03\x03\x00\x00\x03\x00\x07") == b"\x01\x00\x00\x03\x00\x00\x00\x07"


@pytest.mark.parametrize("extradata", [b"", b"\x01", b"\x01\x64\x00\x28\xff\xe1\x00\x09\x67\x64", b"garbage"])
def test_malformed_parameter_sets_read_as_unknown(extradata):
    assert _read(extradata) is None


def _encode(path, codec, pix_fmt="yuv420p", sar=Fraction(16, 15), options=None):
    with av.open(str(path), "w") as output:
        stream = output.add_stream(codec, rate=25, options=options or {})
        stream.width, stream.height, stream.pix_fmt = 720, 576, pix_fmt
        stream.codec_context.sample_aspect_ratio = sar
        for index in range(2):
            frame = av.VideoFrame.from_ndarray(np.zeros((576, 720, 3), np.uint8), format="rgb24")
            frame.pts, frame.time_base = index, Fraction(1, 25)
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))
    track = VideoTrack(str(path))
    try:
        return track.open_session_video_dict()
    finally:
        track.close()


@pytest.mark.parametrize(("codec", "pix_fmt", "options", "sar"), [
    ("libx264", "yuv420p", {"profile": "baseline"}, Fraction(16, 15)),
    ("libx264", "yuv420p", {}, Fraction(16, 15)),
    ("libx264", "yuv422p", {}, Fraction(16, 15)),
    ("libx264", "yuv444p", {"x264-params": "cqm=jvt"}, Fraction(16, 15)),
    ("libx264", "yuv420p", {"x264-params": "interlaced=1"}, Fraction(64, 45)),
    ("libx264", "yuv420p", {}, Fraction(12, 11)),
    ("libx265", "yuv420p", {"x265-params": "log-level=none"}, Fraction(16, 15)),
])
def test_real_encoder_parameter_sets(tmp_path, codec, pix_fmt, options, sar):
    video = _encode(tmp_path / "clip.mkv", codec, pix_fmt, sar, options)
    import base64

    extradata = base64.b64decode(video["extradata_b64"])
    assert sample_aspect_ratio_from_parameter_sets(video["codec"], extradata, 720, 576) == sar


@pytest.mark.parametrize("codec", ["libx264", "libx265"])
def test_uplink_without_the_field_is_fitted_from_the_bitstream(tmp_path, codec):
    video = _encode(tmp_path / "clip.mkv", codec, options={"x265-params": "log-level=none"} if codec == "libx265" else None)

    async def opened(fields: dict) -> dict:
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        try:
            async with aiohttp.ClientSession() as http, http.ws_connect(
                f"http://127.0.0.1:{server.port}/control"
            ) as ws:
                await ws.send_str(json.dumps({"type": "hello", "protocol_version": 1,
                                              "client_name": "test", "display": {"w": 1920, "h": 1080}}))
                assert json.loads((await ws.receive()).data)["type"] == "capabilities"
                await ws.send_str(json.dumps({
                    "type": "open_session", "source": "uplink", "file": {"name": "clip.mkv"},
                    "video": fields, "model": "passthrough", "quality_tier": "lossless-ffv1",
                    "display": {"w": 1920, "h": 1080},
                }))
                while True:
                    message = json.loads((await asyncio.wait_for(ws.receive(), 30)).data)
                    if message["type"] in ("session_opened", "error"):
                        return message
        finally:
            await server.stop()

    without = {key: value for key, value in video.items() if key != "sample_aspect_ratio"}
    reply = asyncio.run(opened(without))
    assert (reply["downlink_width"], reply["downlink_height"]) == (1440, 1080)
    # An explicit value from the client always wins over the bitstream.
    reply = asyncio.run(opened({**without, "sample_aspect_ratio": [1, 1]}))
    assert (reply["downlink_width"], reply["downlink_height"]) == (1350, 1080)
    # Unreadable parameter sets fall back to square pixels instead of failing.
    reply = asyncio.run(opened({**without, "extradata_b64": "AAAA"}))
    assert reply["type"] == "session_opened"
    assert (reply["downlink_width"], reply["downlink_height"]) == (1350, 1080)
