"""Display mapping on the client (PROTOCOL §3.6): the display target asked
of a capable server, at open and with every seek, the mapping read back for
each epoch, and the uplink's HDR10+ flag."""

import argparse
import asyncio
from fractions import Fraction
from types import SimpleNamespace

import av
import pytest

from relay_client_core import DisplayTarget, RelayClient, SessionConfig
from relay_client_core import client as module
from relay_client_core.cli import _display_target
from relay_media.demux import VideoTrack
from relay_media.hdr import hevc_nal_length_size, iter_nals, nal_type, sei_nal
from test_hdr_pipeline import _hdr10plus, _write_source

PQ = {"primaries": 9, "transfer": 16, "matrix": 9, "range": "limited"}
SDR = {"primaries": 1, "transfer": 1, "matrix": 1, "range": "limited"}
HDR_DISPLAY = DisplayTarget("pq", 1107, 203)
MAPPED_PQ = {"transfer": "pq", "peak_nits": 1107, "sdr_white_nits": 203, "applied": ["dolby_vision"]}
MAPPED_SDR = {"transfer": "sdr", "sdr_white_nits": 80, "applied": ["dolby_vision"]}


def _open(monkeypatch, cfg, *, capabilities, opened):
    monkeypatch.setattr(
        module, "_open_local_source",
        lambda path: (SimpleNamespace(close=lambda: None),
                      {"codec": "hevc", "width": 64, "height": 64, "time_base": [1, 1000]},
                      None, []),
    )

    async def scenario():
        client = RelayClient("media-server", 8590)
        client.capabilities = capabilities
        sent = {}

        async def request(expect, type_, timeout=30, keepalive=False, **fields):
            sent.update(fields)
            return {
                "session_id": "s", "media_port": 1, "uplink_token": None,
                "downlink_token": "d", "downlink_codec": "hevc",
                "downlink_width": 64, "downlink_height": 64, **opened,
            }

        client._request = request
        try:
            return sent, await client.open_session(cfg)
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_the_display_is_described_to_a_mapping_server(monkeypatch):
    sent, info = _open(
        monkeypatch, SessionConfig(path="a.mkv", high_bit_depth="hdr", display_target=HDR_DISPLAY),
        capabilities={"high_bit_depth": True, "display_mapping": True},
        opened={"downlink_bit_depth": 10, "downlink_color": PQ, "downlink_mapping": MAPPED_PQ},
    )
    assert sent["display_target"] == {"transfer": "pq", "peak_nits": 1107, "sdr_white_nits": 203}
    assert info.display_target == HDR_DISPLAY
    assert info.downlink_mapping == MAPPED_PQ and info.downlink_hdr and info.hdr_source


def test_an_sdr_mapped_stream_is_still_an_hdr_source(monkeypatch):
    _sent, info = _open(
        monkeypatch,
        SessionConfig(path="a.mkv", high_bit_depth="hdr", display_target=DisplayTarget("sdr", None, 80)),
        capabilities={"high_bit_depth": True, "display_mapping": True},
        opened={"downlink_bit_depth": 10, "downlink_color": SDR, "downlink_mapping": MAPPED_SDR},
    )
    assert not info.downlink_hdr and info.hdr_source


@pytest.mark.parametrize(("capabilities", "high_bit_depth"), [
    ({"high_bit_depth": True}, "hdr"),                            # no display mapping
    ({"high_bit_depth": True, "display_mapping": True}, None),     # 8-bit asked for
    ({"display_mapping": True}, "hdr"),                            # no deep colour at all
])
def test_the_target_is_sent_only_where_it_can_apply(monkeypatch, capabilities, high_bit_depth):
    sent, info = _open(
        monkeypatch,
        SessionConfig(path="a.mkv", high_bit_depth=high_bit_depth, display_target=HDR_DISPLAY),
        capabilities=capabilities, opened={},
    )
    assert "display_target" not in sent
    assert info.display_target is None and info.downlink_mapping is None


def test_a_static_metadata_source_reads_as_unmapped(monkeypatch):
    _sent, info = _open(
        monkeypatch, SessionConfig(path="a.mkv", high_bit_depth="hdr", display_target=HDR_DISPLAY),
        capabilities={"high_bit_depth": True, "display_mapping": True},
        opened={"downlink_bit_depth": 10, "downlink_color": PQ, "downlink_mapping": None},
    )
    assert info.display_target == HDR_DISPLAY  # negotiated: seeks name the display
    assert info.downlink_mapping is None and info.hdr_source


def _seek_client(session, replies):
    """A client mid-session whose seeks are answered by ``replies``."""
    client = RelayClient.__new__(RelayClient)
    client.session, client.track, client.epoch = session, None, 0
    client._pending, client._uplink_task, client._down_q = {}, None, asyncio.Queue()
    client.buffered_ms = 0
    sent = []

    async def request(expect, type_, timeout=30, keepalive=False, **fields):
        sent.append(fields)
        return {"type": "seek_ready", "epoch": fields["epoch"], **replies.pop(0)}

    client._request = request
    return client, sent


def _session(**fields):
    return module.SessionInfo(
        session_id="s", media_port=1, uplink_token=None, downlink_token="d",
        downlink_codec="hevc", downlink_extradata=None, downlink_width=64, downlink_height=64,
        source="server_file", downlink_color=PQ, **fields,
    )


def test_each_seek_names_the_display_and_reads_back_the_epochs_mapping():
    session = _session(display_target=HDR_DISPLAY, downlink_mapping=MAPPED_PQ)
    client, sent = _seek_client(session, [
        {"downlink_color": SDR, "downlink_mapping": MAPPED_SDR},
        {"downlink_color": SDR, "downlink_mapping": MAPPED_SDR},
    ])
    sdr = DisplayTarget("sdr", None, 80)
    asyncio.run(client.seek(1000, display_target=sdr))
    assert sent[0]["display_target"] == {"transfer": "sdr", "sdr_white_nits": 80}
    assert (session.display_target, session.downlink_mapping, session.downlink_color) == (sdr, MAPPED_SDR, SDR)
    assert not session.downlink_hdr and session.hdr_source
    # A seek that names no display repeats the current one: every epoch is
    # unambiguous, however the server coalesces seeks.
    asyncio.run(client.seek(2000))
    assert sent[1]["display_target"] == {"transfer": "sdr", "sdr_white_nits": 80}


def test_a_superseded_seek_leaves_the_mapping_alone():
    session = _session(display_target=HDR_DISPLAY, downlink_mapping=MAPPED_PQ)
    client, _sent = _seek_client(session, [{"downlink_color": SDR, "downlink_mapping": MAPPED_SDR}])

    async def scenario():
        original = client._request

        async def superseded(*args, **fields):
            reply = await original(*args, **fields)
            client.epoch += 1  # a newer seek went out meanwhile
            return reply

        client._request = superseded
        await client.seek(1000, display_target=DisplayTarget("sdr"))

    asyncio.run(scenario())
    assert (session.downlink_mapping, session.downlink_color) == (MAPPED_PQ, PQ)


def test_sessions_without_mapping_seek_as_before():
    session = _session()
    client, sent = _seek_client(session, [{}])
    asyncio.run(client.seek(1000, display_target=HDR_DISPLAY))
    assert sent == [{"target_pts": 1000, "epoch": 1}]
    assert session.display_target is None and session.downlink_color == PQ


@pytest.mark.parametrize(("target", "mapping", "fits"), [
    (HDR_DISPLAY, MAPPED_PQ, True),
    (HDR_DISPLAY, {**MAPPED_PQ, "peak_nits": 1000}, True),   # the guess before the switch
    (HDR_DISPLAY, {**MAPPED_PQ, "peak_nits": 600}, False),   # far too dim
    (DisplayTarget("pq", 600), MAPPED_PQ, False),            # brighter than the display
    (HDR_DISPLAY, {**MAPPED_PQ, "peak_nits": None}, True),
    (DisplayTarget("pq"), MAPPED_PQ, True),
    (HDR_DISPLAY, MAPPED_SDR, False),
    (DisplayTarget("sdr", None, 80), MAPPED_SDR, True),
    (DisplayTarget("sdr"), {**MAPPED_SDR, "sdr_white_nits": 203}, True),
    (DisplayTarget("sdr"), MAPPED_PQ, False),
    (HDR_DISPLAY, None, False),
])
def test_which_mapping_fits_which_display(target, mapping, fits):
    assert target.fits(mapping) is fits


def test_wire_form_leaves_out_what_is_unknown():
    assert DisplayTarget("pq", 1107.04, 203).to_wire() == {"transfer": "pq", "peak_nits": 1107.0,
                                                           "sdr_white_nits": 203}
    assert DisplayTarget("sdr").to_wire() == {"transfer": "sdr"}


@pytest.mark.parametrize(("text", "expected"), [
    ("pq:1107:203", DisplayTarget("pq", 1107, 203)),
    ("pq:1000", DisplayTarget("pq", 1000)),
    ("sdr", DisplayTarget("sdr")),
    ("sdr:80", DisplayTarget("sdr", None, 80)),
])
def test_cli_display_target(text, expected):
    assert _display_target(text) == expected


@pytest.mark.parametrize("text", ["pq", "hdr:1000", "pq:bright", "sdr:80:203"])
def test_cli_rejects_other_targets(text):
    with pytest.raises(argparse.ArgumentTypeError):
        _display_target(text)


def _with_hdr10plus_on_first_keyframe(source, path):
    """``source`` remuxed with an HDR10+ message before the first keyframe's
    first slice, as an HDR10+ stream carries it."""
    with av.open(str(source)) as src, av.open(str(path), "w") as out:
        in_stream = src.streams.video[0]
        out_stream = out.add_stream_from_template(in_stream)
        length_size = hevc_nal_length_size(bytes(in_stream.codec_context.extradata or b""))
        first = True
        for packet in src.demux(in_stream):
            if not packet.size:
                continue  # the demuxer's closing empty packet
            if first and packet.is_keyframe:
                nals = list(iter_nals(bytes(packet), length_size))
                slice_at = next(i for i, nal in enumerate(nals) if nal_type(nal) < 32)
                nals.insert(slice_at, sei_nal(4, _hdr10plus(400)))
                payload = b"".join(len(nal).to_bytes(length_size, "big") + nal for nal in nals)
                replacement = av.Packet(len(payload))
                replacement.update(payload)
                replacement.pts, replacement.dts = packet.pts, packet.dts
                replacement.time_base, replacement.is_keyframe = packet.time_base, True
                packet, first = replacement, False
            packet.stream = out_stream
            out.mux(packet)


def test_the_uplink_reports_hdr10plus_from_its_first_keyframe(tmp_path):
    plain = tmp_path / "hdr10.mkv"
    _write_source(plain)
    tagged = tmp_path / "hdr10plus.mkv"
    _with_hdr10plus_on_first_keyframe(plain, tagged)
    for path, expected in ((plain, False), (tagged, True)):
        track, video, _duration, _chapters = module._open_local_source(str(path))
        try:
            assert video.get("hdr10plus", False) is expected, path.name
            # The check reads through a container of its own: the uplink
            # still starts at the first packet.
            first = next(track.packets())
            assert first.pts == 0 and first.keyframe
        finally:
            track.close()


def test_only_pq_hevc_is_read_for_hdr10plus(tmp_path, monkeypatch):
    sdr = tmp_path / "sdr.mkv"
    _write_source(sdr, tags=(1, 1, 1, 1), x265_params="lossless=1")
    opened = []
    real_open = av.open
    monkeypatch.setattr(av, "open", lambda *a, **k: opened.append(a) or real_open(*a, **k))
    track = VideoTrack(str(sdr))
    try:
        assert track.first_keyframe_hdr10plus() is False
        assert len(opened) == 1  # the track's own container only
    finally:
        track.close()
