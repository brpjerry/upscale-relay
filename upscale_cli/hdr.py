"""HDR signalling that a re-encode would otherwise lose.

Colour tags (primaries, transfer, matrix, range) travel through
``upscale_cli.color``. Everything else a player needs to show HDR as
mastered lives in the HEVC bitstream, and an encoder fed raw frames writes
none of it:

* static metadata: the mastering display colour volume and content light
  level SEI messages (SMPTE ST 2086, CTA-861.3), from the decoded frames'
  side data or from the source container;
* dynamic metadata: HDR10+ (an ITU-T T.35 user-data SEI message per frame)
  and the Dolby Vision RPU of profile 8 (a NAL unit of type 62 at the end
  of each access unit).

PyAV cannot hand static metadata to an encoder (``decoded_side_data`` and
``coded_side_data`` are read-only), so every message is written into the
encoded Annex B packets here instead, the same way for NVENC and x265. The
Matroska muxer converts the packets to length-prefixed NAL units and keeps
every unit. Dynamic messages are copied byte for byte from the source
packet with the same PTS: the upscale does not change which scene a frame
belongs to, and an RPU's reshaping still applies to the re-encoded base
layer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# ITU-T H.273 code points (the FFmpeg enum values).
PRIMARIES_BT2020 = 9
TRANSFER_PQ = 16  # SMPTE ST 2084
TRANSFER_HLG = 18  # ARIB STD-B67
HDR_TRANSFERS = frozenset({TRANSFER_PQ, TRANSFER_HLG})

# HEVC NAL unit types and SEI payload types (ITU-T H.265 7.4.2.2, D.2.1).
_NAL_PREFIX_SEI = 39
_NAL_DOVI_RPU = 62  # UNSPEC62, Dolby Vision's RPU
_NAL_FIRST_NON_VCL = 32
_SEI_T35 = 4
_SEI_MASTERING_DISPLAY = 137
_SEI_CONTENT_LIGHT_LEVEL = 144
# T.35 header of an HDR10+ message: country (United States), provider
# (Samsung), provider-oriented code, application identifier 4.
_HDR10PLUS_T35_PREFIX = b"\xb5\x00\x3c\x00\x01\x04"
_START_CODE = b"\x00\x00\x00\x01"

# Dolby Vision profiles whose RPU still describes a re-encoded base layer.
# Profile 8 carries a backward-compatible HDR10 (8.1), SDR (8.2) or HLG
# (8.4) base layer and no enhancement layer. Profile 7's RPU composes an
# enhancement layer the relay drops, and profile 5's base layer is not a
# displayable signal without the RPU's reshaping.
DOVI_RPU_PROFILES = frozenset({8})

# AVMasteringDisplayMetadata: AVRational display_primaries[3][2] (R, G, B),
# white_point[2], min_luminance, max_luminance; int has_primaries,
# has_luminance. AVContentLightMetadata: unsigned MaxCLL, MaxFALL.
_AV_MASTERING = struct.Struct("<20i2i")
_AV_LIGHT_LEVEL = struct.Struct("<2I")


def is_hdr_transfer(transfer: int) -> bool:
    return int(transfer) in HDR_TRANSFERS


@dataclass(frozen=True)
class MasteringDisplay:
    """ST 2086 colour volume in the units the SEI carries: chromaticities
    in 0.00002, luminance in 0.0001 cd/m2. Primaries are red, green, blue."""

    primaries: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]
    white_point: tuple[int, int]
    max_luminance: int
    min_luminance: int

    @classmethod
    def from_side_data(cls, data: bytes) -> "MasteringDisplay | None":
        """From FFmpeg's AVMasteringDisplayMetadata (frame or stream side data)."""
        if len(data) < _AV_MASTERING.size:
            return None
        v = _AV_MASTERING.unpack_from(data)
        has_primaries, has_luminance = v[20], v[21]
        if not (has_primaries and has_luminance):
            return None

        def q(num: int, den: int, unit: int) -> int:
            return round(num * unit / den) if den else 0

        rgb = tuple((q(v[i], v[i + 1], 50000), q(v[i + 2], v[i + 3], 50000)) for i in (0, 4, 8))
        return cls(
            primaries=rgb,
            white_point=(q(v[12], v[13], 50000), q(v[14], v[15], 50000)),
            min_luminance=q(v[16], v[17], 10000),
            max_luminance=q(v[18], v[19], 10000),
        )

    @classmethod
    def from_wire(cls, value) -> "MasteringDisplay | None":
        """From ``open_session.video.hdr.mastering_display`` (docs/PROTOCOL.md)."""
        try:
            primaries = tuple((int(x), int(y)) for x, y in value["primaries"])
            white = tuple(int(c) for c in value["white_point"])
            result = cls(
                primaries=primaries, white_point=white,
                max_luminance=int(value["max_luminance"]),
                min_luminance=int(value["min_luminance"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if len(primaries) != 3 or len(white) != 2:
            return None
        if not all(0 <= c <= 50000 for pair in (*primaries, white) for c in pair):
            return None
        if not 0 <= result.min_luminance < result.max_luminance <= 0xFFFFFFFF:
            return None
        return result

    def to_wire(self) -> dict:
        return {
            "primaries": [list(p) for p in self.primaries],
            "white_point": list(self.white_point),
            "max_luminance": self.max_luminance,
            "min_luminance": self.min_luminance,
        }

    def sei_payload(self) -> bytes:
        red, green, blue = self.primaries
        # H.265 D.3.28 orders the primaries green, blue, red.
        return struct.pack(
            ">8H2I", *green, *blue, *red, *self.white_point,
            self.max_luminance, self.min_luminance,
        )


@dataclass(frozen=True)
class ContentLightLevel:
    max_cll: int
    max_fall: int

    @classmethod
    def from_side_data(cls, data: bytes) -> "ContentLightLevel | None":
        if len(data) < _AV_LIGHT_LEVEL.size:
            return None
        max_cll, max_fall = _AV_LIGHT_LEVEL.unpack_from(data)
        return cls(max_cll, max_fall)

    @classmethod
    def from_wire(cls, value) -> "ContentLightLevel | None":
        try:
            result = cls(int(value["max_cll"]), int(value["max_fall"]))
        except (KeyError, TypeError, ValueError):
            return None
        return result if result.max_cll >= 0 and result.max_fall >= 0 else None

    def to_wire(self) -> dict:
        return {"max_cll": self.max_cll, "max_fall": self.max_fall}

    def sei_payload(self) -> bytes:
        return struct.pack(">2H", min(self.max_cll, 0xFFFF), min(self.max_fall, 0xFFFF))


@dataclass(frozen=True)
class StaticHdr:
    """HDR10 static metadata; either half may be unknown."""

    mastering_display: MasteringDisplay | None = None
    content_light_level: ContentLightLevel | None = None

    def __bool__(self) -> bool:
        return self.mastering_display is not None or self.content_light_level is not None

    @classmethod
    def from_side_data(cls, side_data) -> "StaticHdr":
        """From a mapping of FFmpeg side data names or types to bytes: a
        stream's ``coded_side_data`` or a decoded frame's ``side_data``."""
        mastering = light = None
        for key, value in side_data.items():
            name = str(getattr(key, "name", key)).lower()
            raw = bytes(value)
            if "mastering_display" in name:
                mastering = MasteringDisplay.from_side_data(raw)
            elif "content_light_level" in name:
                light = ContentLightLevel.from_side_data(raw)
        return cls(mastering, light)

    @classmethod
    def from_wire(cls, value) -> "StaticHdr":
        if not isinstance(value, dict):
            return cls()
        mastering = value.get("mastering_display")
        light = value.get("content_light_level")
        return cls(
            MasteringDisplay.from_wire(mastering) if isinstance(mastering, dict) else None,
            ContentLightLevel.from_wire(light) if isinstance(light, dict) else None,
        )

    def to_wire(self) -> dict:
        wire = {}
        if self.mastering_display is not None:
            wire["mastering_display"] = self.mastering_display.to_wire()
        if self.content_light_level is not None:
            wire["content_light_level"] = self.content_light_level.to_wire()
        return wire

    def or_else(self, fallback: "StaticHdr") -> "StaticHdr":
        """Each half from self where known, else from ``fallback``."""
        return StaticHdr(
            self.mastering_display or fallback.mastering_display,
            self.content_light_level or fallback.content_light_level,
        )

    def sei_nals(self) -> list[bytes]:
        nals = []
        if self.mastering_display is not None:
            nals.append(sei_nal(_SEI_MASTERING_DISPLAY, self.mastering_display.sei_payload()))
        if self.content_light_level is not None:
            nals.append(sei_nal(_SEI_CONTENT_LIGHT_LEVEL, self.content_light_level.sei_payload()))
        return nals


@dataclass(frozen=True)
class DolbyVisionConfig:
    """The fields of a DOVIDecoderConfigurationRecord the relay acts on."""

    profile: int
    level: int = 0
    bl_signal_compatibility_id: int = 0

    @classmethod
    def from_side_data(cls, data: bytes) -> "DolbyVisionConfig | None":
        """From FFmpeg's AVDOVIDecoderConfigurationRecord (``dovi_conf``):
        version major, minor, profile, level, rpu/el/bl present, compat id."""
        if len(data) < 8 or not data[4]:  # no RPU: nothing to carry
            return None
        return cls(profile=data[2], level=data[3], bl_signal_compatibility_id=data[7])

    @classmethod
    def from_wire(cls, value) -> "DolbyVisionConfig | None":
        if not isinstance(value, dict):
            return None
        try:
            return cls(
                profile=int(value["profile"]),
                level=int(value.get("level", 0)),
                bl_signal_compatibility_id=int(value.get("bl_signal_compatibility_id", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def to_wire(self) -> dict:
        return {
            "profile": self.profile, "level": self.level,
            "bl_signal_compatibility_id": self.bl_signal_compatibility_id,
        }

    @property
    def rpu_survives_reencode(self) -> bool:
        return self.profile in DOVI_RPU_PROFILES


@dataclass(frozen=True, slots=True)
class DynamicHdr:
    """One frame's dynamic metadata, as it is to be written back."""

    hdr10plus: tuple[bytes, ...] = ()  # T.35 SEI payloads (unescaped)
    dovi_rpu: bytes | None = None  # whole RPU NAL unit, header included

    def __bool__(self) -> bool:
        return bool(self.hdr10plus) or self.dovi_rpu is not None


# -- NAL units ----------------------------------------------------------------


def escape_rbsp(rbsp: bytes) -> bytes:
    """Insert emulation prevention bytes (H.265 7.4.2)."""
    out = bytearray()
    zeros = 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def unescape_rbsp(data: bytes) -> bytes:
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _sei_size_bytes(value: int) -> bytes:
    return b"\xff" * (value // 255) + bytes([value % 255])


def sei_nal(payload_type: int, payload: bytes) -> bytes:
    """A prefix SEI NAL unit (header 0x4E01) holding one message."""
    rbsp = _sei_size_bytes(payload_type) + _sei_size_bytes(len(payload)) + payload + b"\x80"
    return b"\x4e\x01" + escape_rbsp(rbsp)


def nal_type(nal: bytes) -> int:
    return (nal[0] >> 1) & 0x3F


def hevc_nal_length_size(extradata: bytes | None) -> int | None:
    """NAL length field size of hvcC extradata; None for Annex B streams."""
    if not extradata or len(extradata) < 23:
        return None
    if extradata.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
        return None
    return (extradata[21] & 3) + 1


def iter_nals(data: bytes, length_size: int | None):
    """NAL units of one packet: length-prefixed, or Annex B when
    ``length_size`` is None. Malformed tails end the iteration."""
    if length_size is not None:
        pos, end = 0, len(data)
        while pos + length_size <= end:
            size = int.from_bytes(data[pos:pos + length_size], "big")
            pos += length_size
            if size == 0 or pos + size > end:
                return
            yield data[pos:pos + size]
            pos += size
        return
    for start, stop in _annexb_spans(data):
        yield data[start:stop]


def _annexb_spans(data: bytes) -> list[tuple[int, int]]:
    """(start, stop) of each NAL unit after its start code; a trailing zero
    byte of a following four-byte start code is excluded."""
    starts = []
    pos = data.find(b"\x00\x00\x01")
    while pos >= 0:
        starts.append(pos + 3)
        pos = data.find(b"\x00\x00\x01", pos + 3)
    spans = []
    for index, start in enumerate(starts):
        stop = starts[index + 1] - 3 if index + 1 < len(starts) else len(data)
        while stop > start and data[stop - 1] == 0:
            stop -= 1
        if stop > start:
            spans.append((start, stop))
    return spans


def _sei_messages(nal: bytes):
    """(payload_type, payload) of each message in a prefix SEI NAL unit."""
    rbsp = unescape_rbsp(nal[2:])
    pos, end = 0, len(rbsp)
    while pos < end and rbsp[pos:] != b"\x80":
        payload_type = 0
        while pos < end and rbsp[pos] == 0xFF:
            payload_type += 255
            pos += 1
        if pos >= end:
            return
        payload_type += rbsp[pos]
        pos += 1
        size = 0
        while pos < end and rbsp[pos] == 0xFF:
            size += 255
            pos += 1
        if pos >= end:
            return
        size += rbsp[pos]
        pos += 1
        if pos + size > end:
            return
        yield payload_type, rbsp[pos:pos + size]
        pos += size


def extract_dynamic_hdr(
    packet: bytes, length_size: int | None, *, dovi_rpu: bool,
) -> DynamicHdr | None:
    """HDR10+ messages and (when ``dovi_rpu``) the Dolby Vision RPU of one
    source access unit, or None when it carries neither."""
    hdr10plus: list[bytes] = []
    rpu = None
    for nal in iter_nals(packet, length_size):
        if len(nal) < 3:
            continue
        kind = nal_type(nal)
        if kind == _NAL_PREFIX_SEI:
            hdr10plus.extend(
                payload for payload_type, payload in _sei_messages(nal)
                if payload_type == _SEI_T35 and payload.startswith(_HDR10PLUS_T35_PREFIX)
            )
        elif kind == _NAL_DOVI_RPU and dovi_rpu:
            rpu = bytes(nal)
    if not hdr10plus and rpu is None:
        return None
    return DynamicHdr(tuple(hdr10plus), rpu)


def inject_hevc_metadata(
    packet: bytes, *, prefix: list[bytes] = (), suffix: list[bytes] = (),
) -> bytes | None:
    """Annex B access unit with ``prefix`` NAL units placed before its first
    slice and ``suffix`` units after its last; None when the packet is not
    Annex B (nothing to anchor them to)."""
    if not packet.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
        return None
    insert_at = None
    for start, _stop in _annexb_spans(packet):
        if nal_type(packet[start:start + 1]) < _NAL_FIRST_NON_VCL:
            insert_at = start - 3
            # Include the leading zero of a four-byte start code.
            if insert_at > 0 and packet[insert_at - 1] == 0:
                insert_at -= 1
            break
    if insert_at is None:
        return None
    parts = [packet[:insert_at]]
    parts += [_START_CODE + nal for nal in prefix]
    parts.append(packet[insert_at:])
    parts += [_START_CODE + nal for nal in suffix]
    return b"".join(parts)


def dynamic_hdr_nals(meta: DynamicHdr) -> tuple[list[bytes], list[bytes]]:
    """(prefix, suffix) NAL units that write ``meta`` back: HDR10+ before
    the slices, the RPU last in the access unit as Dolby Vision requires."""
    prefix = [sei_nal(_SEI_T35, payload) for payload in meta.hdr10plus]
    suffix = [meta.dovi_rpu] if meta.dovi_rpu is not None else []
    return prefix, suffix
