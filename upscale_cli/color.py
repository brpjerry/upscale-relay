"""Colour description shared by relay and offline encoding.

Encoded output has to say how its YUV is to be read: matrix, range,
primaries and transfer. Untagged, a player guesses from the *output* size,
so an upscaled SD source is read as BT.709 and a full-range one as limited.

The description comes from the source and is resolved once. Tags the source
leaves unspecified are filled in as mpv fills them for the source's own size
(video/csputils.c mp_csp_guess_colorspace / mp_csp_guess_primaries; limited
range; BT.709 transfer), so a relayed picture matches the original played
locally and only the upscale differs. The same description drives every
YUV<->RGB conversion and the encoder's tags.
"""

from __future__ import annotations

from dataclasses import dataclass

# FFmpeg enum values (libavutil/pixfmt.h).
_SPC_RGB, _SPC_BT709, _SPC_FCC, _SPC_BT470BG, _SPC_SMPTE170M, _SPC_SMPTE240M = 0, 1, 4, 5, 6, 7
_SPC_BT2020_NCL, _SPC_BT2020_CL = 9, 10
_RANGE_LIMITED, _RANGE_FULL = 1, 2
_PRI_BT709, _PRI_BT470BG, _PRI_SMPTE170M = 1, 5, 6
_TRC_BT709 = 1
_UNSPECIFIED = (0, 2)  # 0 is "reserved" for primaries/transfer, 2 "unspecified"

# libswscale coefficient set (SWS_CS_*) for each matrix it converts.
_SWS_MATRIX = {
    _SPC_BT709: 1, _SPC_FCC: 4, _SPC_BT470BG: 5, _SPC_SMPTE170M: 5,
    _SPC_SMPTE240M: 7, _SPC_BT2020_NCL: 9, _SPC_BT2020_CL: 9,
}


def _is_hd(width: int, height: int) -> bool:
    return width >= 1280 or height > 576


@dataclass(frozen=True)
class VideoColor:
    """How YUV samples are to be read, as FFmpeg enum values."""

    matrix: int
    range: int
    primaries: int
    transfer: int

    @classmethod
    def resolve(cls, tags, width: int, height: int) -> "VideoColor":
        """From anything carrying FFmpeg colour tags (a frame or codec context)."""
        return cls.resolve_values(
            int(tags.colorspace), int(tags.color_range),
            int(tags.color_primaries), int(tags.color_trc), width, height,
        )

    @classmethod
    def resolve_values(
        cls, matrix: int, color_range: int, primaries: int, transfer: int,
        width: int, height: int,
    ) -> "VideoColor":
        """Fill unspecified tags as mpv would for a ``width`` x ``height`` source.

        RGB and matrices libswscale cannot convert get the guessed YUV matrix,
        since the output is YUV either way.
        """
        hd = _is_hd(width, height)
        if matrix not in _SWS_MATRIX:
            matrix = _SPC_BT709 if hd else (_SPC_BT470BG if height == 576 else _SPC_SMPTE170M)
        if color_range not in (_RANGE_LIMITED, _RANGE_FULL):
            color_range = _RANGE_LIMITED
        if primaries in _UNSPECIFIED:
            if hd:
                primaries = _PRI_BT709
            elif height == 576:
                primaries = _PRI_BT470BG
            elif height in (480, 486):
                primaries = _PRI_SMPTE170M
            else:
                primaries = _PRI_BT709
        if transfer in _UNSPECIFIED:
            transfer = _TRC_BT709
        return cls(matrix, color_range, primaries, transfer)

    @property
    def _sws_matrix(self) -> int:
        return _SWS_MATRIX[self.matrix]

    def tag(self, target) -> None:
        """Write this description onto an encoder codec context or a frame."""
        target.colorspace = self.matrix
        target.color_range = self.range
        target.color_primaries = self.primaries
        target.color_trc = self.transfer

    def to_rgb(self, reformatter, frame):
        """rgb24 of a frame whose YUV this describes (RGB frames pass through)."""
        if frame.format.is_rgb:
            return reformatter.reformat(frame, format="rgb24")
        return reformatter.reformat(
            frame, format="rgb24",
            src_colorspace=self._sws_matrix, src_color_range=self.range,
        )

    def to_output(self, reformatter, frame, *, width=None, height=None,
                  pix_fmt: str, interpolation=None):
        """Scale and convert an RGB frame, or a YUV frame this describes, to
        the encoder's YUV format with this matrix and range."""
        if frame.format.is_rgb:
            return reformatter.reformat(
                frame, width=width, height=height, format=pix_fmt,
                dst_colorspace=self._sws_matrix, dst_color_range=self.range,
                interpolation=interpolation,
            )
        return reformatter.reformat(
            frame, width=width, height=height, format=pix_fmt,
            src_colorspace=self._sws_matrix, dst_colorspace=self._sws_matrix,
            src_color_range=self.range, dst_color_range=self.range,
            interpolation=interpolation,
        )
