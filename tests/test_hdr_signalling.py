"""HEVC HDR metadata written back into re-encoded packets (relay_media.hdr)."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

from relay_media.demux import coded_side_data
from relay_server.pipeline import output_bit_depth
from upscale_cli.color import VideoColor, fill_unspecified
from relay_media.hdr import (
    ContentLightLevel,
    DolbyVisionConfig,
    DynamicHdr,
    MasteringDisplay,
    StaticHdr,
    _sei_messages,
    dynamic_hdr_nals,
    escape_rbsp,
    extract_dynamic_hdr,
    has_hdr10plus,
    hevc_nal_length_size,
    inject_hevc_metadata,
    iter_nals,
    nal_type,
    sei_nal,
    tags_from_wire,
    tags_to_wire,
    unescape_rbsp,
)

# NAL headers (type << 1, layer 0, temporal id 1).
VPS, SPS, PPS, IDR, TRAIL = b"\x40\x01", b"\x42\x01", b"\x44\x01", b"\x26\x01", b"\x02\x01"
HDR10PLUS = b"\xb5\x00\x3c\x00\x01\x04" + bytes(range(1, 20))
RPU = b"\x7c\x01\x19\x08\x09\x08\x40\x61\x36\x50\x6f"
# AVMasteringDisplayMetadata of a P3-D65 grade at 1000/0.0001 nits, as
# FFmpeg's HEVC decoder exports it (chromaticities over 50000, luminance
# over 10000), followed by has_primaries and has_luminance.
P3_MASTERING = struct.pack(
    "<22i",
    34000, 50000, 16000, 50000, 13250, 50000, 34500, 50000, 7500, 50000, 3000, 50000,
    15635, 50000, 16450, 50000, 1, 10000, 10000000, 10000, 1, 1,
)


def _annexb(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + nal for nal in nals)


def _length_prefixed(*nals: bytes) -> bytes:
    return b"".join(len(nal).to_bytes(4, "big") + nal for nal in nals)


@pytest.mark.parametrize("rbsp", [
    b"\x00\x00\x00\x01", b"\x00\x00\x03", b"\x00\x00\x01\x00\x00\x02", b"\x01\x00\x00", b"",
])
def test_emulation_prevention_round_trips(rbsp):
    escaped = escape_rbsp(rbsp)
    assert b"\x00\x00\x00" not in escaped and b"\x00\x00\x01" not in escaped
    assert unescape_rbsp(escaped) == rbsp


def test_sei_messages_round_trip_through_long_sizes():
    payload = bytes(300) + b"\x01"  # zeros need escaping; size needs 0xFF bytes
    nal = sei_nal(4, payload)
    assert nal_type(nal) == 39
    assert list(_sei_messages(nal)) == [(4, payload)]


def test_mastering_display_sei_orders_primaries_green_blue_red():
    mastering = MasteringDisplay.from_side_data(P3_MASTERING)
    assert mastering.primaries == ((34000, 16000), (13250, 34500), (7500, 3000))
    assert (mastering.max_luminance, mastering.min_luminance) == (10000000, 1)
    assert struct.unpack(">8H2I", mastering.sei_payload()) == (
        13250, 34500, 7500, 3000, 34000, 16000, 15635, 16450, 10000000, 1,
    )
    assert MasteringDisplay.from_wire(mastering.to_wire()) == mastering


def test_static_metadata_prefers_the_bitstream_and_falls_back_per_half():
    container = StaticHdr(content_light_level=ContentLightLevel(1000, 400))
    frame = StaticHdr.from_side_data({"MASTERING_DISPLAY_METADATA": P3_MASTERING})
    merged = frame.or_else(container)
    assert merged.mastering_display is not None
    assert merged.content_light_level == ContentLightLevel(1000, 400)
    assert StaticHdr.from_wire(merged.to_wire()) == merged
    # Light level SEI saturates at 16 bits.
    assert ContentLightLevel(70000, 5).sei_payload() == b"\xff\xff\x00\x05"


@pytest.mark.parametrize("wire", [
    {"mastering_display": {"primaries": [[1, 2]], "white_point": [1, 2],
                           "max_luminance": 10, "min_luminance": 1}},
    {"mastering_display": {"primaries": [[1, 2], [3, 4], [5, 60000]], "white_point": [1, 2],
                           "max_luminance": 10, "min_luminance": 1}},
    {"content_light_level": {"max_cll": "x", "max_fall": 1}},
    # JSON's 1e309 parses as inf, which int() refuses with OverflowError.
    {"mastering_display": {"primaries": [[float("inf"), 2], [3, 4], [5, 6]],
                           "white_point": [1, 2], "max_luminance": 10, "min_luminance": 1}},
    {"content_light_level": {"max_cll": float("inf"), "max_fall": 1}},
    "nonsense",
])
def test_malformed_wire_metadata_reads_as_absent(wire):
    assert not StaticHdr.from_wire(wire)


def test_malformed_dolby_vision_and_side_data_read_as_absent():
    assert DolbyVisionConfig.from_wire({"profile": float("inf")}) is None
    assert DolbyVisionConfig.from_wire({"level": 6}) is None
    # PyAV releases before CodecContext.coded_side_data.
    assert coded_side_data(SimpleNamespace()) == {}


def test_dolby_vision_record_from_ffmpeg_side_data():
    p81 = DolbyVisionConfig.from_side_data(b"\x01\x00\x08\x06\x01\x00\x01\x01\x00")
    assert p81 == DolbyVisionConfig(profile=8, level=6, bl_signal_compatibility_id=1)
    assert p81.rpu_survives_reencode
    assert not DolbyVisionConfig(profile=7).rpu_survives_reencode
    assert not DolbyVisionConfig(profile=5).rpu_survives_reencode
    # No RPU present: nothing to carry.
    assert DolbyVisionConfig.from_side_data(b"\x01\x00\x08\x06\x00\x00\x01\x01\x00") is None


@pytest.mark.parametrize("framing", ["length-prefixed", "annexb"])
def test_dynamic_metadata_is_read_from_source_access_units(framing):
    other_t35 = b"\xb5\x00\x31" + b"GA94"  # A/53 captions: not HDR10+
    sei = sei_nal(4, HDR10PLUS)[:-1] + escape_rbsp(b"\x04" + bytes([len(other_t35)]) + other_t35) + b"\x80"
    nals = (sei, IDR + b"\xaf\x00\x00\x03\x01", RPU)
    if framing == "annexb":
        packet, length_size = _annexb(*nals), None
    else:
        packet, length_size = _length_prefixed(*nals), 4
    assert list(iter_nals(packet, length_size)) == list(nals)

    meta = extract_dynamic_hdr(packet, length_size, dovi_rpu=True)
    assert meta == DynamicHdr(hdr10plus=(HDR10PLUS,), dovi_rpu=RPU)
    # Without a Dolby Vision profile 8 source, its RPU stays behind.
    assert extract_dynamic_hdr(packet, length_size, dovi_rpu=False) == DynamicHdr((HDR10PLUS,), None)
    assert extract_dynamic_hdr(_annexb(IDR + b"\xaf"), None, dovi_rpu=True) is None


def test_an_uplink_reports_hdr10plus_from_its_first_keyframe():
    """open_session.video.hdr10plus (PROTOCOL §3.6): mapped sessions decode
    differently, so the server must know at open."""
    hvcc = bytes(21) + bytes([3]) + bytes(10)  # length-prefixed, as demuxed from MKV/MP4
    slice_ = IDR + bytes([0xAF])
    keyframe = (VPS + bytes([0x0C]), SPS + bytes([1]), PPS + bytes([0xC1]), sei_nal(4, HDR10PLUS), slice_)
    assert has_hdr10plus(_length_prefixed(*keyframe), "hevc", hvcc)
    assert has_hdr10plus(_annexb(*keyframe), "hevc", None)
    assert not has_hdr10plus(_length_prefixed(slice_, RPU), "hevc", hvcc)  # Dolby Vision alone
    assert not has_hdr10plus(_annexb(*keyframe), "h264") and not has_hdr10plus(b"", "hevc")


def test_metadata_is_placed_around_the_slices_of_an_access_unit():
    packet = _annexb(VPS + b"\x0c", SPS + b"\x01", PPS + b"\xc1", IDR + b"\xaf\x01", IDR + b"\x80\x02")
    prefix, suffix = dynamic_hdr_nals(DynamicHdr((HDR10PLUS,), RPU))
    static = StaticHdr(MasteringDisplay.from_side_data(P3_MASTERING), ContentLightLevel(564, 277))
    out = inject_hevc_metadata(packet, prefix=static.sei_nals() + prefix, suffix=suffix)

    nals = list(iter_nals(out, None))
    assert [nal_type(nal) for nal in nals] == [32, 33, 34, 39, 39, 39, 19, 19, 62]
    assert [list(_sei_messages(nal))[0][0] for nal in nals[3:6]] == [137, 144, 4]
    assert nals[-1] == RPU
    # Length-prefixed packets have no start code to anchor on.
    assert inject_hevc_metadata(_length_prefixed(IDR), prefix=prefix) is None


def test_hvcc_and_annexb_extradata_select_the_nal_framing():
    hvcc = bytes(21) + b"\x03" + bytes(10)  # lengthSizeMinusOne = 3
    assert hevc_nal_length_size(hvcc) == 4
    assert hevc_nal_length_size(_annexb(VPS, SPS, PPS) + bytes(20)) is None
    assert hevc_nal_length_size(None) is None


_PQ = VideoColor.resolve_values(9, 1, 9, 16, 3840, 2160)
_HLG = VideoColor.resolve_values(9, 1, 9, 18, 3840, 2160)
_SDR = VideoColor.resolve_values(1, 1, 1, 1, 1920, 1080)


@pytest.mark.parametrize(("request_mode", "color", "depth", "expected"), [
    (None, _PQ, 10, 8),     # old clients keep the 8-bit downlink
    ("hdr", _PQ, 10, 10),
    ("hdr", _HLG, 10, 10),
    ("hdr", _SDR, 10, 8),   # 10-bit SDR only on request
    ("all", _SDR, 10, 10),
    ("all", _SDR, 8, 8),    # nothing deeper to keep
    ("all", None, None, 8),  # unknown source
    ("hdr", _PQ, 8, 10),    # an 8-bit PQ source is still HDR
])
def test_output_bit_depth(request_mode, color, depth, expected):
    assert output_bit_depth(request_mode, color, depth) == expected


def test_colour_tags_round_trip_through_the_wire():
    assert tags_to_wire((9, 1, 9, 16)) == {"primaries": 9, "transfer": 16, "matrix": 9, "range": "limited"}
    assert tags_from_wire(tags_to_wire((9, 1, 9, 16))) == (9, 1, 9, 16)
    assert tags_from_wire(tags_to_wire((2, 0, 2, 2))) == (2, 0, 2, 2)  # unspecified stays so
    assert tags_from_wire({"primaries": 9, "transfer": 16}) is None
    assert tags_from_wire({"primaries": 900, "transfer": 16, "matrix": 9}) is None
    assert tags_from_wire({"primaries": float("inf"), "transfer": 16, "matrix": 9}) is None
    assert tags_from_wire({"primaries": 9, "transfer": 16, "matrix": 9, "range": []}) is None
    assert tags_from_wire({"primaries": 9, "transfer": 16, "matrix": 9, "range": "tv"}) is None


def test_frame_tags_win_where_they_are_specified():
    # (matrix, range, primaries, transfer); 2 and 0 are unspecified.
    assert fill_unspecified((2, 0, 2, 2), (9, 1, 9, 16)) == (9, 1, 9, 16)
    assert fill_unspecified((9, 1, 9, 18), (9, 2, 9, 14)) == (9, 1, 9, 18)
    assert fill_unspecified((0, 1, 0, 0), (9, 1, 9, 16)) == (0, 1, 9, 16)  # matrix 0 is RGB
