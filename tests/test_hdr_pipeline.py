"""Deep-colour relay runs: 10-bit HEVC in, 10-bit HEVC out with its HDR
metadata, on generated media (x265 stands in for NVENC, which CI lacks).

The sources are 10-bit PQ/BT.2020 x265 streams whose encoder wrote the
mastering display and light level SEI. Each packet additionally gets an
HDR10+ message and a Dolby Vision RPU NAL unit of its own, as a profile 8.1
source carries them, so the test can tell that every frame's metadata came
back on that frame.
"""

from __future__ import annotations

import base64
import io
import sys
import threading
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

from relay_media import VideoTrack
from relay_protocol import FLAG_EOS, MediaPacket
from relay_server.pipeline import Pipeline, VideoConfig
from upscale_cli.hdr import (
    DolbyVisionConfig,
    StaticHdr,
    escape_rbsp,
    hevc_nal_length_size,
    iter_nals,
    nal_type,
    sei_nal,
)

ROOT = Path(__file__).resolve().parents[1]
PQ_TAGS = (9, 1, 9, 16)
X265_HDR10 = (
    "lossless=1:hdr10=1:max-cll=564,277:"
    "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
)


def _write_source(path, *, tags=PQ_TAGS, x265_params=X265_HDR10, frames=8, size=(64, 64)):
    """A 10-bit x265 source with a different gradient in every frame."""
    width, height = size
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx265", rate=24, options={
            "x265-params": x265_params, "preset": "ultrafast",
        })
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p10le"
        cc = stream.codec_context
        cc.colorspace, cc.color_range, cc.color_primaries, cc.color_trc = tags
        ramp = np.linspace(64, 940, width, dtype=np.uint16)
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p10le")
            luma = np.broadcast_to((ramp + index * 7).clip(64, 940), (height, width))
            chroma = np.full((height // 2, width // 2), 512 + index, np.uint16)
            for plane, values in zip(frame.planes, (luma, chroma, chroma)):
                rows = np.zeros((plane.height, plane.line_size // 2), np.uint16)
                rows[:, :values.shape[1]] = values
                plane.update(rows.tobytes())
            frame.pts, frame.time_base = index, Fraction(1, 24)
            output.mux(stream.encode(frame))
        output.mux(stream.encode(None))


def _hdr10plus(peak: int) -> bytes:
    """A one-window HDR10+ (SMPTE ST 2094-40) message, T.35 header included."""
    fields = [(1, 8), (1, 2), (peak, 27), (0, 1)]
    fields += [(50000, 17)] * 3 + [(20000, 17), (1, 4), (50, 7), (30000, 17), (5, 10)]
    fields += [(0, 1), (0, 1), (0, 1)]
    bits = "".join(format(value, f"0{width}b") for value, width in fields)
    bits += "0" * (-len(bits) % 8)
    return b"\xb5\x00\x3c\x00\x01\x04" + int(bits, 2).to_bytes(len(bits) // 8, "big")


def _rpu(index: int) -> bytes:
    """Stands in for a Dolby Vision RPU NAL unit: type 62, unique per frame.
    The relay copies it byte for byte; decoders reject its body, which is
    fine, so the test reads it back from the bitstream."""
    return b"\x7c\x01" + escape_rbsp(b"\x19\x08\x09" + bytes([index + 1]) * 12) + b"\x80"


def _with_dynamic_metadata(payload: bytes, length_size: int, index: int) -> bytes:
    """One source access unit as a Dolby Vision 8.1 + HDR10+ stream has it:
    HDR10+ SEI before the first slice, the RPU last."""
    nals = list(iter_nals(payload, length_size))
    first_slice = next(i for i, nal in enumerate(nals) if nal_type(nal) < 32)
    nals.insert(first_slice, sei_nal(4, _hdr10plus(400 + index)))
    nals.append(_rpu(index))
    return b"".join(len(nal).to_bytes(length_size, "big") + nal for nal in nals)


def _config(track: VideoTrack, video: dict, dolby_vision=True) -> VideoConfig:
    """What the session builds for a server file (relay_server.session)."""
    return VideoConfig(
        codec=video["codec"],
        extradata=base64.b64decode(video["extradata_b64"]),
        width=video["width"], height=video["height"],
        time_base=Fraction(*video["time_base"]),
        avg_rate=Fraction(*video["avg_rate"]) if video.get("avg_rate") else None,
        color_tags=track.color_tags, bit_depth=track.bit_depth,
        hdr_static=StaticHdr.from_wire(video.get("hdr")),
        dolby_vision=DolbyVisionConfig(8, 6, 1) if dolby_vision else None,
    )


def _relay(source, *, high_bit_depth, model=None, tier="lossless-hevc", display=None,
           dynamic=True, dolby_vision=True):
    """Relay one epoch; returns (downlink bytes, source packets as fed, pipeline)."""
    track = VideoTrack(str(source))
    video = track.open_session_video_dict()
    cfg = _config(track, video, dolby_vision)
    length_size = hevc_nal_length_size(cfg.extradata)
    emitted, errors, done = [], [], threading.Event()

    def emit(packet):
        emitted.append(packet)
        if packet.eos:
            done.set()

    def on_error(message):
        errors.append(message)
        done.set()

    pipeline = Pipeline(
        cfg, model, tier, display or (cfg.width, cfg.height), emit, on_error, ep="cpu",
        lossless_hevc_profile="x265-ultrafast", high_bit_depth=high_bit_depth,
    )
    fed = []
    pipeline.relayed_with = pipeline.upscaler  # close() lets go of it
    try:
        for index, info in enumerate(track.packets()):
            if dynamic:
                info.payload = _with_dynamic_metadata(info.payload, length_size, index)
            fed.append(info)
            pipeline.feed(track.media_packet(info, 0))
        pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=0))
        assert done.wait(60), "pipeline produced no EOS"
    finally:
        pipeline.close()
        track.close()
    assert errors == []
    return b"".join(packet.payload for packet in emitted), fed, cfg, pipeline


def _decode(packets, cfg: VideoConfig):
    """The source as the server's own decoder sees it, by PTS."""
    decoder = av.CodecContext.create(cfg.codec, "r")
    decoder.extradata = cfg.extradata
    frames = {}
    for info in packets:
        packet = av.Packet(len(info.payload))
        packet.update(info.payload)
        packet.pts, packet.dts, packet.time_base = info.pts, info.dts, cfg.time_base
        for frame in decoder.decode(packet):
            frames[frame.pts] = frame
    for frame in decoder.decode(None):
        frames[frame.pts] = frame
    return frames


def _side_data(frame) -> dict[str, bytes]:
    return {key.name: bytes(value) for key, value in frame.side_data.items()}


def _planes(frame) -> list[np.ndarray]:
    return [np.frombuffer(plane, np.uint16).reshape(plane.height, -1)[:, :plane.width]
            for plane in frame.planes]


def test_hdr_source_keeps_10_bits_and_every_frames_metadata(tmp_path):
    source = tmp_path / "pq.mkv"
    _write_source(source)

    downlink, fed, cfg, pipeline = _relay(source, high_bit_depth="hdr")

    reference = _decode(fed, cfg)
    # Packets were given RPUs in decode order; frames come back in display
    # order. Both containers count milliseconds.
    expected_rpu = {info.pts: _rpu(index) for index, info in enumerate(fed)}
    with av.open(io.BytesIO(downlink)) as relayed:
        stream = relayed.streams.video[0]
        assert (stream.codec_context.profile, stream.codec_context.format.name) == (
            "Main 10", "yuv420p10le")
        rpus = {}
        for packet in relayed.demux(stream):
            if packet.size:
                rpus[packet.pts] = [nal for nal in iter_nals(bytes(packet), 4) if nal_type(nal) == 62]
        relayed.seek(0)
        frames = list(relayed.decode(stream))
    assert len(frames) == len(reference) == 8
    for frame in frames:
        source_frame = reference[frame.pts]
        assert frame.format.name == "yuv420p10le"
        assert (int(frame.colorspace), int(frame.color_range),
                int(frame.color_primaries), int(frame.color_trc)) == PQ_TAGS
        assert all(np.array_equal(a, b) for a, b in zip(_planes(frame), _planes(source_frame)))
        relayed_sd, source_sd = _side_data(frame), _side_data(source_frame)
        for kind in ("MASTERING_DISPLAY_METADATA", "CONTENT_LIGHT_LEVEL", "DYNAMIC_HDR_PLUS"):
            assert relayed_sd.get(kind) is not None, kind
            assert relayed_sd[kind] == source_sd[kind], kind
        assert rpus[frame.pts] == [expected_rpu[frame.pts]]
    report = pipeline.hdr_metadata_report()
    assert report["dovi_rpu_frames"] == report["hdr10plus_frames"] == 8
    assert report["static"]["content_light_level"] == {"max_cll": 564, "max_fall": 277}


def test_rpu_of_a_profile_without_a_reusable_base_layer_is_dropped(tmp_path):
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=3)

    downlink, _fed, _cfg, pipeline = _relay(source, high_bit_depth="hdr", dolby_vision=False)

    assert not pipeline.dolby_vision_rpu
    with av.open(io.BytesIO(downlink)) as relayed:
        nals = [nal for packet in relayed.demux(video=0) if packet.size
                for nal in iter_nals(bytes(packet), 4)]
    assert not any(nal_type(nal) == 62 for nal in nals)
    assert any(nal_type(nal) == 39 for nal in nals)  # HDR10+ and static SEI stay


def test_clients_that_do_not_ask_keep_the_8_bit_stream(tmp_path):
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=2)

    downlink, _fed, _cfg, pipeline = _relay(source, high_bit_depth=None, dynamic=False)

    assert pipeline.bit_depth == 8 and pipeline.hdr_metadata_report() is None
    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert frame.format.name == "yuv420p"
    assert int(frame.color_trc) == 16  # tags were always carried
    assert "MASTERING_DISPLAY_METADATA" not in _side_data(frame)


@pytest.mark.parametrize(("mode", "expected"), [("hdr", "yuv420p"), ("all", "yuv420p10le")])
def test_10_bit_sdr_stays_10_bit_only_on_request(tmp_path, mode, expected):
    source = tmp_path / "sdr10.mkv"
    _write_source(source, tags=(1, 1, 1, 1), x265_params="lossless=1", frames=2)

    downlink, _fed, _cfg, pipeline = _relay(source, high_bit_depth=mode, dynamic=False)

    assert pipeline.hdr_metadata_report() is None  # SDR: nothing to write back
    with av.open(io.BytesIO(downlink)) as relayed:
        assert next(relayed.decode(video=0)).format.name == expected


def test_model_runs_on_16_bit_rgb_for_a_deep_colour_session(tmp_path):
    pytest.importorskip("onnxruntime")
    sys.path.insert(0, str(ROOT))
    from tools.make_test_model import build

    model = tmp_path / "bilinear2x.onnx"
    build(model)
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=2)

    downlink, fed, cfg, pipeline = _relay(
        source, high_bit_depth="hdr", model=str(model), tier="lossless-ffv1",
        display=(128, 128), dynamic=False,
    )

    assert pipeline.relayed_with.io_bits == 16 and pipeline.relayed_with.uint8_io
    source_luma = _planes(_decode(fed, cfg)[0])[0]
    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert (frame.width, frame.height, frame.format.name) == (128, 128, "yuv420p10le")
    luma = _planes(frame)[0]
    # A bilinear 2x of a horizontal ramp: every other output column lands
    # between two source columns. 10-bit accuracy, not 8-bit: an 8-bit
    # round trip would be off by up to two 10-bit steps from rounding alone.
    expected = np.repeat(source_luma, 2, axis=0)[:, :].astype(int)
    assert np.abs(luma[:, ::2].astype(int)[:, 1:-1] - expected[:, 1:-1]).max() <= 6
    assert np.unique(luma[32]).size > 64  # the ramp kept its 10-bit steps
