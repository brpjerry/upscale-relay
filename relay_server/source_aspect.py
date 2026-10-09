"""Sample aspect ratio of an uplink source from its codec parameter sets.

Clients send ``open_session.video.sample_aspect_ratio`` when their demuxer
knows it. Some cannot see an aspect declared only in the video bitstream
(Android's MediaExtractor reads none from Matroska), so the server falls
back to the parameter sets in ``extradata_b64``. The output size has to be
announced before any frame is decoded: HEVC's decoder exports the aspect
from hvcC when it opens, H.264's only once a slice activates the SPS, so
H.264 SPS VUI is read here directly (ITU-T H.264 7.3.2.1.1 and E.1.1).
"""

from __future__ import annotations

import logging
from fractions import Fraction

log = logging.getLogger("relay.session")

# ITU-T H.264 / H.265 Table E-1: aspect_ratio_idc -> sample aspect ratio.
_ASPECT_RATIO_IDC = {
    1: (1, 1), 2: (12, 11), 3: (10, 11), 4: (16, 11), 5: (40, 33), 6: (24, 11),
    7: (20, 11), 8: (32, 11), 9: (80, 33), 10: (18, 11), 11: (15, 11), 12: (64, 33),
    13: (160, 99), 14: (4, 3), 15: (3, 2), 16: (2, 1),
}
_EXTENDED_SAR = 255
# profile_idc values whose SPS carries chroma format, bit depth and scaling lists.
_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


def sample_aspect_ratio_from_parameter_sets(
    codec: str, extradata: bytes | None, width: int, height: int,
) -> Fraction | None:
    """The bitstream's sample aspect ratio, or None when it cannot be read."""
    if not extradata:
        return None
    try:
        if codec == "h264":
            return _h264_sample_aspect_ratio(extradata)
        if codec == "hevc":
            return _decoder_sample_aspect_ratio(codec, extradata, width, height)
    except Exception as error:  # malformed client data must not fail the open
        log.info("could not read the %s sample aspect ratio: %s", codec, error)
    return None


def _decoder_sample_aspect_ratio(codec, extradata, width, height) -> Fraction | None:
    import av

    ctx = av.CodecContext.create(codec, "r")
    ctx.extradata = extradata
    ctx.width, ctx.height = width, height
    ctx.open()
    sar = ctx.sample_aspect_ratio
    return Fraction(sar.numerator, sar.denominator) if sar and sar > 0 else None


def _h264_sample_aspect_ratio(extradata: bytes) -> Fraction | None:
    for nal in _h264_sps_units(extradata):
        sar = _sps_sample_aspect_ratio(_rbsp(nal[1:]))
        if sar is not None:
            return sar
    return None


def _h264_sps_units(extradata: bytes):
    """SPS NAL units from avcC (ISO/IEC 14496-15) or Annex B extradata."""
    if extradata[0] == 1:  # avcC configurationVersion
        count = extradata[5] & 0x1F
        offset = 6
        for _ in range(count):
            length = int.from_bytes(extradata[offset:offset + 2], "big")
            nal = extradata[offset + 2:offset + 2 + length]
            offset += 2 + length
            if nal and nal[0] & 0x1F == 7:
                yield nal
        return
    for nal in extradata.replace(b"\x00\x00\x00\x01", b"\x00\x00\x01").split(b"\x00\x00\x01"):
        nal = nal.rstrip(b"\x00")
        if nal and nal[0] & 0x1F == 7:
            yield nal


def _rbsp(payload: bytes) -> bytes:
    """Drop emulation prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in payload:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data: bytes):
        self._data = data
        self._position = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte = self._data[self._position >> 3]  # IndexError past the end
            value = (value << 1) | ((byte >> (7 - (self._position & 7))) & 1)
            self._position += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("malformed exp-Golomb code")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        code = self.ue()
        return (code + 1) // 2 if code & 1 else -(code // 2)


def _skip_scaling_list(bits: _Bits, size: int) -> None:
    last = next_scale = 8
    for _ in range(size):
        if next_scale:
            next_scale = (last + bits.se() + 256) % 256
        last = next_scale or last


def _sps_sample_aspect_ratio(rbsp: bytes) -> Fraction | None:
    bits = _Bits(rbsp)
    profile_idc = bits.u(8)
    bits.u(16)  # constraint flags, level_idc
    bits.ue()  # seq_parameter_set_id
    if profile_idc in _HIGH_PROFILES:
        chroma_format_idc = bits.ue()
        if chroma_format_idc == 3:
            bits.u(1)  # separate_colour_plane_flag
        bits.ue()  # bit_depth_luma_minus8
        bits.ue()  # bit_depth_chroma_minus8
        bits.u(1)  # qpprime_y_zero_transform_bypass_flag
        if bits.u(1):  # seq_scaling_matrix_present_flag
            for index in range(8 if chroma_format_idc != 3 else 12):
                if bits.u(1):
                    _skip_scaling_list(bits, 16 if index < 6 else 64)
    bits.ue()  # log2_max_frame_num_minus4
    pic_order_cnt_type = bits.ue()
    if pic_order_cnt_type == 0:
        bits.ue()  # log2_max_pic_order_cnt_lsb_minus4
    elif pic_order_cnt_type == 1:
        bits.u(1)  # delta_pic_order_always_zero_flag
        bits.se()  # offset_for_non_ref_pic
        bits.se()  # offset_for_top_to_bottom_field
        for _ in range(bits.ue()):  # num_ref_frames_in_pic_order_cnt_cycle
            bits.se()
    bits.ue()  # max_num_ref_frames
    bits.u(1)  # gaps_in_frame_num_value_allowed_flag
    bits.ue()  # pic_width_in_mbs_minus1
    bits.ue()  # pic_height_in_map_units_minus1
    if not bits.u(1):  # frame_mbs_only_flag
        bits.u(1)  # mb_adaptive_frame_field_flag
    bits.u(1)  # direct_8x8_inference_flag
    if bits.u(1):  # frame_cropping_flag
        for _ in range(4):
            bits.ue()
    if not bits.u(1) or not bits.u(1):  # vui present, aspect_ratio_info present
        return None
    idc = bits.u(8)
    if idc == _EXTENDED_SAR:
        num, den = bits.u(16), bits.u(16)
    else:
        num, den = _ASPECT_RATIO_IDC.get(idc, (0, 0))
    return Fraction(num, den) if num and den else None
