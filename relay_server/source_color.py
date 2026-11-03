"""Colour and sample depth of an uplink source, before any frame decodes.

A deep-colour downlink (docs/PROTOCOL.md ``open_session.high_bit_depth``) is
chosen when the session opens: the encoder, the model's I/O and
``session_opened`` all depend on it. Clients send ``open_session.video.color``
and ``bit_depth`` from their demuxer. For one that does not, HEVC's decoder
exports both from hvcC as it opens, as it does the aspect
(``relay_server.source_aspect``); other codecs stay unknown, which means the
8-bit downlink older clients have always received.
"""

from __future__ import annotations

import logging

from relay_media.hdr import tags_from_wire

log = logging.getLogger("relay.session")


def uplink_color_and_depth(
    video: dict, extradata: bytes | None,
) -> tuple[tuple[int, int, int, int] | None, int | None]:
    """(colour tags, bit depth) of an uplink source, either None if unknown."""
    tags = tags_from_wire(video.get("color"))
    depth = video.get("bit_depth")
    depth = depth if type(depth) is int and 8 <= depth <= 16 else None
    if (tags is None or depth is None) and video.get("codec") == "hevc" and extradata:
        try:
            probed_tags, probed_depth = _decoder_color_and_depth(
                extradata, video["width"], video["height"],
            )
        except Exception as error:  # malformed client data must not fail the open
            log.info("could not read the hevc colour description: %s", error)
        else:
            tags = tags if tags is not None else probed_tags
            depth = depth if depth is not None else probed_depth
    return tags, depth


def bitstream_color(extradata, width, height) -> tuple[int, int, int, int] | None:
    """The colour tags HEVC's decoder exports from the parameter sets in
    ``extradata``, unspecified where they set none; None if unreadable."""
    if not extradata:
        return None
    try:
        tags, _depth = _decoder_color_and_depth(extradata, width, height)
    except Exception as error:
        log.info("could not read the hevc colour description: %s", error)
        return None
    return tags


def _decoder_color_and_depth(extradata, width, height):
    import av

    ctx = av.CodecContext.create("hevc", "r")
    ctx.extradata = extradata
    ctx.width, ctx.height = width, height
    ctx.open()
    fmt = ctx.format
    if fmt is None:  # no sequence parameter set it could read
        return None, None
    depth = max((c.bits for c in fmt.components), default=0)
    tags = (int(ctx.colorspace), int(ctx.color_range), int(ctx.color_primaries), int(ctx.color_trc))
    return tags, (depth or None)
