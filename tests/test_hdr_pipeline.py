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
import dataclasses
import io
import sys
import threading
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest

import relay_server.pipeline as pipeline_module
from relay_media import VideoTrack
from relay_protocol import FLAG_EOS, MediaPacket
from relay_server.pipeline import Pipeline, VideoConfig
from relay_server.source_color import uplink_color_and_depth
from relay_media.hdr import (
    DolbyVisionConfig,
    MasteringDisplay,
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


def _write_source(path, *, tags=PQ_TAGS, x265_params=X265_HDR10, frames=8, size=(64, 64), bits=10):
    """A 10-bit (or 8-bit) x265 source with a different gradient in every frame."""
    width, height = size
    pix_fmt, dtype, step = ("yuv420p10le", np.uint16, 4) if bits == 10 else ("yuv420p", np.uint8, 1)
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx265", rate=24, options={
            "x265-params": x265_params, "preset": "ultrafast",
        })
        stream.width, stream.height, stream.pix_fmt = width, height, pix_fmt
        cc = stream.codec_context
        cc.colorspace, cc.color_range, cc.color_primaries, cc.color_trc = tags
        ramp = np.linspace(16 * step, 235 * step, width).astype(int)
        for index in range(frames):
            frame = av.VideoFrame(width, height, pix_fmt)
            luma = np.broadcast_to((ramp + index * 7).clip(16 * step, 235 * step), (height, width))
            chroma = np.full((height // 2, width // 2), 128 * step + index, dtype)
            for plane, values in zip(frame.planes, (luma, chroma, chroma)):
                rows = np.zeros((plane.height, plane.line_size // np.dtype(dtype).itemsize), dtype)
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


def _config(track: VideoTrack, video: dict) -> VideoConfig:
    """What the session builds for a server file (relay_server.session),
    with the Dolby Vision 8.1 record the test media pretends to have."""
    return VideoConfig(
        codec=video["codec"],
        extradata=base64.b64decode(video["extradata_b64"]),
        width=video["width"], height=video["height"],
        time_base=Fraction(*video["time_base"]),
        avg_rate=Fraction(*video["avg_rate"]) if video.get("avg_rate") else None,
        color_tags=track.color_tags, bit_depth=track.bit_depth,
        hdr_static=StaticHdr.from_wire(video.get("hdr")),
        dolby_vision=DolbyVisionConfig(8, 6, 1),
    )


def _relay(source, *, high_bit_depth, model=None, tier="lossless-hevc", display=None,
           dynamic=True, edit=None, **config):
    """Relay one epoch; returns (downlink bytes, source packets as fed, pipeline).
    ``config`` replaces VideoConfig fields; ``edit(payload, info, index)``
    rewrites each source packet after its dynamic metadata is added."""
    track = VideoTrack(str(source))
    video = track.open_session_video_dict()
    cfg = dataclasses.replace(_config(track, video), **config)
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
            if edit is not None:
                info.payload = edit(info.payload, info, index)
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

    # Profile 7 (FEL/MEL): its RPU composes an enhancement layer.
    downlink, _fed, _cfg, pipeline = _relay(
        source, high_bit_depth="hdr", dolby_vision=DolbyVisionConfig(7, 6, 6),
    )

    assert pipeline.bit_depth == 10 and not pipeline.dolby_vision_rpu
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

    downlink, _fed, _cfg, pipeline = _relay(
        source, high_bit_depth=mode, dynamic=False, dolby_vision=None,
    )

    assert pipeline.hdr_metadata_report() is None  # SDR: nothing to write back
    with av.open(io.BytesIO(downlink)) as relayed:
        assert next(relayed.decode(video=0)).format.name == expected


def _planes_8(frame) -> list[np.ndarray]:
    return [np.frombuffer(plane, np.uint8).reshape(plane.height, -1)[:, :plane.width]
            for plane in frame.planes]


@pytest.mark.parametrize(("mode", "expected"), [("hdr", "yuv420p"), ("all", "yuv420p10le")])
def test_8_bit_sdr_goes_10_bit_under_all(tmp_path, mode, expected):
    source = tmp_path / "sdr8.mkv"
    _write_source(source, tags=(1, 1, 1, 1), x265_params="lossless=1", frames=2, bits=8)

    downlink, fed, cfg, pipeline = _relay(
        source, high_bit_depth=mode, dynamic=False, dolby_vision=None,
    )

    assert pipeline.bit_depth == (10 if mode == "all" else 8)
    assert pipeline.hdr_metadata_report() is None  # SDR: nothing to write back
    reference = _decode(fed, cfg)
    with av.open(io.BytesIO(downlink)) as relayed:
        frames = list(relayed.decode(video=0))
    assert len(frames) == 2
    for frame in frames:
        assert frame.format.name == expected and _tags(frame) == (1, 1, 1, 1)
        source_planes = _planes_8(reference[frame.pts])
        if mode == "hdr":
            assert all(np.array_equal(a, b) for a, b in zip(_planes_8(frame), source_planes))
        else:  # limited range: each 8-bit code is exactly a quarter of its 10-bit one
            assert all(np.array_equal(a, b.astype(np.uint16) * 4)
                       for a, b in zip(_planes(frame), source_planes))


@pytest.mark.parametrize(("mode", "carried"), [("all", True), ("hdr", False)])
def test_profile_8_rpu_over_an_sdr_base_layer_rides_a_10_bit_downlink(tmp_path, mode, carried):
    # Profile 8.2: the RPU maps an SDR base layer to PQ. That base layer is
    # 10-bit SDR, kept at 10 bits only under "all".
    source = tmp_path / "sdr10.mkv"
    _write_source(source, tags=(1, 1, 1, 1), x265_params="lossless=1", frames=3)

    downlink, fed, _cfg, pipeline = _relay(
        source, high_bit_depth=mode, dolby_vision=DolbyVisionConfig(8, 6, 2),
    )

    assert pipeline.dolby_vision_rpu is carried
    with av.open(io.BytesIO(downlink)) as relayed:
        rpus = [nal for packet in relayed.demux(video=0) if packet.size
                for nal in iter_nals(bytes(packet), 4) if nal_type(nal) == 62]
    assert sorted(rpus) == (sorted(_rpu(index) for index in range(len(fed))) if carried else [])


def _tags(frame) -> tuple[int, int, int, int]:
    return (int(frame.colorspace), int(frame.color_range),
            int(frame.color_primaries), int(frame.color_trc))


@pytest.mark.parametrize(("source_tags", "x265_params", "client_tags", "expected"), [
    # The bitstream's transfer wins over the container's, as HLG signalled by
    # an alternative transfer SEI over BT.2020-10 does. The depth was decided
    # from the client's: SDR, so 8 bits.
    (PQ_TAGS, X265_HDR10, (9, 1, 9, 14), (PQ_TAGS, "yuv420p")),
    # No colour description in the bitstream: the client's tags fill it in.
    ((2, 0, 2, 2), "lossless=1", PQ_TAGS, (PQ_TAGS, "yuv420p10le")),
])
def test_uplink_colour_is_the_first_frames_with_the_clients_filling_in(
    tmp_path, source_tags, x265_params, client_tags, expected,
):
    source = tmp_path / "source.mkv"
    _write_source(source, tags=source_tags, x265_params=x265_params, frames=2)

    downlink, _fed, _cfg, _pipeline = _relay(
        source, high_bit_depth="hdr", dynamic=False, dolby_vision=None,
        color_tags=client_tags, color_from_first_frame=True,
    )

    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert (_tags(frame), frame.format.name) == expected


def test_hdr_found_on_the_first_frame_still_gets_its_metadata(tmp_path):
    # An uplink whose colour neither the client sent nor the server probed,
    # under "all": 10 bits for its depth, HDR signalling once a frame is PQ.
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=3)

    downlink, _fed, _cfg, pipeline = _relay(
        source, high_bit_depth="all", dolby_vision=None,
        color_tags=None, color_from_first_frame=True,
    )

    assert pipeline.bit_depth == 10
    report = pipeline.hdr_metadata_report()
    assert report["hdr10plus_frames"] == 3
    assert report["static"]["content_light_level"] == {"max_cll": 564, "max_fall": 277}
    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert _tags(frame) == PQ_TAGS
    assert "MASTERING_DISPLAY_METADATA" in _side_data(frame)


def test_a_server_without_a_10_bit_encoder_streams_8_bits(tmp_path, monkeypatch):
    real_select = pipeline_module.select_encoder

    def eight_bit_only(*args, bit_depth=8, **kwargs):
        if bit_depth > 8:
            raise RuntimeError("no 10-bit encoder")
        return real_select(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "select_encoder", eight_bit_only)
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=2)

    downlink, _fed, _cfg, pipeline = _relay(source, high_bit_depth="hdr")

    assert pipeline.bit_depth == 8 and not pipeline.dolby_vision_rpu
    assert pipeline.hdr_metadata_report() is None
    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert frame.format.name == "yuv420p" and int(frame.color_trc) == 16


# A brighter grade than X265_HDR10's, from the second source GOP on.
REGRADED = MasteringDisplay(
    primaries=((34000, 16000), (13250, 34500), (7500, 3000)),
    white_point=(15635, 16450), max_luminance=40000000, min_luminance=50,
)


def _regraded_from_second_gop(payload: bytes, info, _index) -> bytes:
    if not (info.keyframe and info.pts):
        return payload
    # Placed after the encoder's own SEI, so the decoder takes this one.
    nals = list(iter_nals(payload, 4))
    first_slice = next(i for i, nal in enumerate(nals) if nal_type(nal) < 32)
    nals.insert(first_slice, sei_nal(137, REGRADED.sei_payload()))
    return b"".join(len(nal).to_bytes(4, "big") + nal for nal in nals)


def test_static_metadata_follows_the_source_frame_at_each_keyframe(tmp_path, monkeypatch):
    real_select = pipeline_module.select_encoder

    def short_gops(*args, **kwargs):
        codec, pix_fmt, options = real_select(*args, **kwargs)
        return codec, pix_fmt, {**options, "g": "24"}

    monkeypatch.setattr(pipeline_module, "select_encoder", short_gops)
    source = tmp_path / "pq.mkv"
    _write_source(
        source, frames=60,
        x265_params=X265_HDR10 + ":keyint=24:min-keyint=24:scenecut=0:open-gop=0",
    )

    downlink, _fed, _cfg, pipeline = _relay(
        source, high_bit_depth="hdr", dynamic=False, edit=_regraded_from_second_gop,
    )

    with av.open(io.BytesIO(downlink)) as relayed:
        frames = list(relayed.decode(video=0))
    assert len(frames) == 60
    for frame in frames:
        mastering = MasteringDisplay.from_side_data(
            _side_data(frame)["MASTERING_DISPLAY_METADATA"],
        )
        # Frame 24 starts the second GOP, at 1000 ms.
        expected = 40000000 if frame.pts >= 1000 else 10000000
        assert mastering.max_luminance == expected, frame.pts
    assert pipeline.hdr_metadata_report()["static"]["mastering_display"] == REGRADED.to_wire()


def test_uplink_colour_and_depth_from_the_client_else_the_parameter_sets(tmp_path):
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=1)
    track = VideoTrack(str(source))
    try:
        video = track.open_session_video_dict()
    finally:
        track.close()
    extradata = base64.b64decode(video["extradata_b64"])
    bare = {key: value for key, value in video.items() if key not in ("color", "bit_depth")}

    assert uplink_color_and_depth(video, extradata) == (PQ_TAGS, 10)
    # Android sends neither; HEVC parameter sets carry both.
    assert uplink_color_and_depth(bare, extradata) == (PQ_TAGS, 10)
    # Malformed values read as absent.
    malformed = {**bare, "color": {**video["color"], "range": []}, "bit_depth": 1e309}
    assert uplink_color_and_depth(malformed, extradata) == (PQ_TAGS, 10)
    # Only HEVC is probed, and parameter sets it cannot read do not fail the open.
    assert uplink_color_and_depth({**bare, "codec": "h264"}, extradata) == (None, None)
    assert uplink_color_and_depth(bare, b"\x01" + bytes(40)) == (None, None)


def test_frame_stage_api_runs_16_bit_models(tmp_path):
    pytest.importorskip("onnxruntime")
    sys.path.insert(0, str(ROOT))
    from tools.make_test_model import build
    from upscale_cli.infer import OnnxUpscaler

    model = tmp_path / "bilinear2x.onnx"
    build(model)
    source = tmp_path / "pq.mkv"
    _write_source(source, frames=1)
    with av.open(str(source)) as container:
        frame = next(container.decode(video=0))

    (out,) = OnnxUpscaler(str(model), ep="cpu", io_bits=16).process(frame)

    assert (out.width, out.height, out.format.name) == (128, 128, "rgb48le")
    assert np.unique(out.to_ndarray()[32, :, 1]).size > 64  # not 8-bit steps


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


def test_8_bit_source_runs_the_16_bit_model_under_all(tmp_path):
    pytest.importorskip("onnxruntime")
    sys.path.insert(0, str(ROOT))
    from tools.make_test_model import build

    model = tmp_path / "bilinear2x.onnx"
    build(model)
    source = tmp_path / "sdr8.mkv"
    _write_source(source, tags=(1, 1, 1, 1), x265_params="lossless=1", frames=2, bits=8)

    downlink, fed, cfg, pipeline = _relay(
        source, high_bit_depth="all", model=str(model), tier="lossless-ffv1",
        display=(128, 128), dynamic=False, dolby_vision=None,
    )

    assert pipeline.relayed_with.io_bits == 16
    source_luma = _planes_8(_decode(fed, cfg)[0])[0].astype(int) * 4
    with av.open(io.BytesIO(downlink)) as relayed:
        frame = next(relayed.decode(video=0))
    assert (frame.format.name, _tags(frame)) == ("yuv420p10le", (1, 1, 1, 1))
    luma = _planes(frame)[0].astype(int)
    # The source's codes at 10 bits (a bilinear 2x keeps the mean: no
    # darkening), and in between 10-bit codes no 8-bit stream could carry.
    expected = np.repeat(source_luma, 2, axis=0)
    assert np.abs(luma[:, ::2][:, 1:-1] - expected[:, 1:-1]).max() <= 6
    assert abs(luma.mean() - source_luma.mean()) < 0.5
    assert (luma % 4 != 0).any()
