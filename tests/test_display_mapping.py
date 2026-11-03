"""Display-mapped HDR on the server (PROTOCOL §3.6): which epochs are mapped
for which display, how a mapped epoch is tagged and carried, remapping on
seek, and the session's reports. A fake mapper decodes in process; one test
runs the real ffmpeg + libplacebo where one can map."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import os
import shutil
import threading
import time
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

import av
import pytest

from ports import free_port_pair
from relay_client_core import DisplayTarget as ClientTarget
from relay_client_core import RelayClient, SessionConfig
from relay_media import VideoTrack
from relay_media.hdr import (
    DolbyVisionConfig,
    hevc_nal_length_size,
    hevc_parameter_sets,
    iter_nals,
    nal_type,
    sei_nal,
    to_annexb,
)
from relay_protocol import FLAG_EOS, MediaPacket
from relay_server import display_mapping
from relay_server.display_mapping import (
    DisplayMapper,
    DisplayTarget,
    EpochMapping,
    MapperError,
    SourceDynamics,
    decide,
)
from relay_server.pipeline import Pipeline
from relay_server.server import RelayServer
from test_hdr_pipeline import PQ_TAGS, _config, _hdr10plus, _with_dynamic_metadata, _write_source

ROOT = Path(__file__).resolve().parents[1]
SDR_TAGS = (1, 1, 1, 1)
P81 = DolbyVisionConfig(8, 6, 1)


def _tags(frame) -> tuple[int, int, int, int]:
    return (int(frame.colorspace), int(frame.color_range),
            int(frame.color_primaries), int(frame.color_trc))


# -- the mapping table ----------------------------------------------------------


@pytest.mark.parametrize(("wire", "expected"), [
    ({"transfer": "pq", "peak_nits": 1107, "sdr_white_nits": 203}, DisplayTarget("pq", 1107.0, 203.0)),
    ({"transfer": "sdr"}, DisplayTarget("sdr")),
    ({"transfer": "pq", "peak_nits": float("inf"), "sdr_white_nits": True}, DisplayTarget("pq")),
    ({"transfer": "pq", "peak_nits": -1, "sdr_white_nits": "203"}, DisplayTarget("pq")),
    ({"transfer": "hlg"}, None),
    ({"peak_nits": 1000}, None),
    ("pq", None),
    (None, None),
])
def test_display_target_from_the_wire(wire, expected):
    assert DisplayTarget.from_wire(wire) == expected


@pytest.mark.parametrize(("source", "sdr", "pq"), [
    (SourceDynamics("hevc", P81), ("dolby_vision",), ()),
    (SourceDynamics("hevc", DolbyVisionConfig(8, 6, 4)), ("dolby_vision",), ()),  # 8.4, HLG base
    (SourceDynamics("hevc", DolbyVisionConfig(7, 6, 6)), ("dolby_vision",), ()),
    (SourceDynamics("hevc", DolbyVisionConfig(5, 6, 0)), ("dolby_vision",), ("dolby_vision",)),
    (SourceDynamics("hevc", None, hdr10plus=True), ("hdr10plus",), ()),
    (SourceDynamics("hevc", P81, hdr10plus=True), ("dolby_vision",), ()),  # Dolby Vision wins
    (SourceDynamics("hevc", DolbyVisionConfig(8, 6, 2)), (), ()),  # 8.2: an SDR base layer
    (SourceDynamics("hevc"), (), ()),  # HDR10, HLG, SDR
    (SourceDynamics("h264", P81, hdr10plus=True), (), ()),
])
def test_what_each_target_applies(source, sdr, pq):
    assert (source.applied_for("sdr"), source.applied_for("pq")) == (sdr, pq)
    assert source.mappable == bool(sdr or pq)
    for transfer, applied in (("sdr", sdr), ("pq", pq)):
        mapping = decide(source, DisplayTarget(transfer, 1107))
        assert mapping == (EpochMapping(transfer, applied) if source.mappable else None)
    assert decide(source, None) is None


def test_a_mapping_reports_itself_and_picks_its_curve():
    dovi = EpochMapping("sdr", ("dolby_vision",))
    assert dovi.to_wire() == {
        "transfer": "sdr", "peak_nits": None, "sdr_white_nits": None, "applied": ["dolby_vision"],
    }
    assert EpochMapping("pq", ()).to_wire()["applied"] == []
    assert not EpochMapping("pq", ()).mapped
    graph = dovi.filter_graph()
    assert "color_trc=bt709" in graph and "tonemapping=st2094-10" in graph
    assert "apply_dolbyvision=1" in graph and "tone_map_metadata=cie_y" in graph
    graph = EpochMapping("sdr", ("hdr10plus",)).filter_graph()
    assert "tonemapping=st2094-40" in graph and "tone_map_metadata=hdr10plus" in graph
    assert "color_trc=smpte2084" in EpochMapping("pq", ("dolby_vision",)).filter_graph()
    assert "peak_detect=0" in graph and "format=yuv420p10le" in graph


def test_hvcc_parameter_sets_go_in_band_for_an_annex_b_decoder():
    vps, sps, pps, sei = b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc1", b"\x4e\x01\x05"

    def array(kind, *nals):
        return bytes([kind]) + len(nals).to_bytes(2, "big") + b"".join(
            len(nal).to_bytes(2, "big") + nal for nal in nals)

    hvcc = bytes(21) + b"\x03" + b"\x04" + array(32, vps) + array(33, sps) + array(34, pps) + array(39, sei)
    assert hevc_parameter_sets(hvcc) == [vps, sps, pps, sei]
    assert hevc_parameter_sets(hvcc[:-2]) == [vps, sps, pps]  # malformed tail dropped
    annexb = b"\x00\x00\x00\x01" + vps + b"\x00\x00\x01" + sps
    assert hevc_parameter_sets(annexb) == [vps, sps]
    assert hevc_parameter_sets(None) == hevc_parameter_sets(b"\x01\x02") == []

    slice_ = b"\x26\x01\xaf"
    packet = len(slice_).to_bytes(4, "big") + slice_
    assert to_annexb(packet, 4, [vps]) == b"\x00\x00\x00\x01" + vps + b"\x00\x00\x00\x01" + slice_
    assert list(iter_nals(to_annexb(packet, 4), None)) == [slice_]


# -- the pipeline, through a mapper that decodes in process ----------------------


class _FakeProcess:
    """MapperProcess's interface: decodes the Annex B it is fed (proving the
    in-band parameter sets suffice) and delivers frames tagged as the mapping
    would make them. The pictures themselves stay the source's."""

    def __init__(self, owner, mapping, *, codec, extradata, width, height, time_base):
        self.mapping, self.owner = mapping, owner
        self._length_size = hevc_nal_length_size(extradata)
        self._parameter_sets = hevc_parameter_sets(extradata)
        self._decoder = av.CodecContext.create(codec, "r")  # no extradata
        self._time_base = time_base
        self.alive, self.started = True, False
        self.held = []

    def start(self, on_frame, on_item, on_error):
        self.started = True
        self._on_frame, self._on_item = on_frame, on_item

    def _deliver(self, frames):
        for frame in frames:
            mapped = frame.reformat(format="yuv420p10le")
            mapped.colorspace, mapped.color_range, mapped.color_primaries, mapped.color_trc = self.mapping.tags
            mapped.pts, mapped.time_base = frame.pts, self._time_base
            self._on_frame(mapped)

    def send(self, payload, pts, keyframe):
        self.owner.sent.append(payload)
        data = to_annexb(payload, self._length_size, self._parameter_sets if keyframe else ())
        packet = av.Packet(len(data))
        packet.update(data)
        packet.pts, packet.time_base = pts, self._time_base
        self._deliver(self._decoder.decode(packet))

    def hold(self, item):
        self._on_item(item)

    def finish(self):
        self._deliver(self._decoder.decode(None))
        self.alive = False

    def kill(self):
        self.alive = False

    close = kill


class _StuckProcess(_FakeProcess):
    """An ffmpeg with seconds of packets queued: a write waits until it dies."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dead = threading.Event()
        self.writing = threading.Event()

    @property
    def killed(self):
        return self._dead.is_set()

    def send(self, payload, pts, keyframe):
        self.writing.set()
        if not self._dead.wait(30):
            raise AssertionError("the seek never killed the mapper")
        raise MapperError("display mapping: writing to ffmpeg failed: socket closed")

    def kill(self):
        self.alive = False
        self._dead.set()

    close = kill


class _FakeMapper:
    def __init__(self, process=_FakeProcess):
        self.spawned: list[EpochMapping] = []
        self.sent: list[bytes] = []
        self.processes = []
        self._process = process

    def spawn(self, mapping, **source):
        self.spawned.append(mapping)
        self.processes.append(self._process(self, mapping, **source))
        return self.processes[-1]


def _dynamic_source(tmp_path, frames=6):
    """A PQ source as Dolby Vision 8.1 + HDR10+ carry it (test_hdr_pipeline)."""
    path = tmp_path / "pq.mkv"
    _write_source(path, frames=frames)
    return path


class _Relay:
    """A pipeline fed epoch by epoch, its downlink kept per epoch."""

    def __init__(self, source, mapper, target, **config):
        self.track = VideoTrack(str(source))
        video = self.track.open_session_video_dict()
        self.cfg = dataclasses.replace(_config(self.track, video), **config)
        self.length_size = hevc_nal_length_size(self.cfg.extradata)
        self.downlink: dict[int, list[bytes]] = defaultdict(list)
        self.errors, self.eos = [], threading.Event()
        self.pipeline = Pipeline(
            self.cfg, None, "lossless-hevc", (self.cfg.width, self.cfg.height),
            self._emit, self._error, ep="cpu", lossless_hevc_profile="x265-ultrafast",
            high_bit_depth="hdr", display_mapper=mapper, display_target=target,
        )

    def _emit(self, packet):
        if packet.eos:
            self.eos.set()
        else:
            self.downlink[packet.epoch].append(packet.payload)

    def _error(self, message):
        self.errors.append(message)
        self.eos.set()

    def play(self, epoch):
        self.eos.clear()
        fed = []
        for index, info in enumerate(self.track.packets(from_pts=0)):
            info.payload = _with_dynamic_metadata(info.payload, self.length_size, index)
            fed.append(info.pts)
            self.pipeline.feed(self.track.media_packet(info, epoch))
        self.pipeline.feed(MediaPacket(payload=b"", flags=FLAG_EOS, epoch=epoch))
        assert self.eos.wait(60), "no EOS"
        assert self.errors == []
        return sorted(fed)

    def epoch_stream(self, epoch):
        """(frames, every NAL unit type) of one epoch's Matroska file."""
        with av.open(io.BytesIO(b"".join(self.downlink[epoch]))) as relayed:
            kinds = [nal_type(nal) for packet in relayed.demux(video=0) if packet.size
                     for nal in iter_nals(bytes(packet), 4)]
            relayed.seek(0)
            return list(relayed.decode(video=0)), kinds

    def close(self):
        self.pipeline.close()
        self.track.close()


def test_a_mapped_epoch_is_the_mappings_colour_with_nothing_dynamic_left(tmp_path):
    mapper = _FakeMapper()
    relay = _Relay(_dynamic_source(tmp_path), mapper, DisplayTarget("sdr"), dolby_vision=P81)
    try:
        assert relay.pipeline.downlink_mapping == {
            "transfer": "sdr", "peak_nits": None, "sdr_white_nits": None,
            "applied": ["dolby_vision"],
        }
        assert relay.pipeline.downlink_color == {
            "primaries": 1, "transfer": 1, "matrix": 1, "range": "limited"}
        assert relay.pipeline.bit_depth == 10 and not relay.pipeline.dolby_vision_rpu
        source_pts = relay.play(0)
        frames, kinds = relay.epoch_stream(0)
    finally:
        relay.close()
    assert [frame.pts for frame in frames] == source_pts  # PTS equivalence
    assert {_tags(frame) for frame in frames} == {SDR_TAGS}
    assert {frame.format.name for frame in frames} == {"yuv420p10le"}
    assert 62 not in kinds and 39 not in kinds  # no RPU, HDR10+ or static SEI
    # Every source packet went to the mapper, dynamic metadata and all, and
    # a spare for the next epoch was started beside it.
    assert len(mapper.sent) == len(source_pts)
    assert any(nal_type(nal) == 62 for nal in iter_nals(mapper.sent[0], relay.length_size))
    assert mapper.spawned == [EpochMapping("sdr", ("dolby_vision",))] * 2


def test_a_seek_remaps_only_when_the_transfer_changes(tmp_path):
    mapper = _FakeMapper()
    relay = _Relay(_dynamic_source(tmp_path, frames=3), mapper, DisplayTarget("sdr"), dolby_vision=P81)
    try:
        relay.play(0)
        # Fullscreen on an HDR display: profile 8.1 streams as §3.5 there.
        relay.pipeline.flush(1, 0, DisplayTarget("pq", 1107, 203))
        assert relay.pipeline.downlink_mapping["applied"] == []
        assert relay.pipeline.downlink_mapping["transfer"] == "pq"
        assert relay.pipeline.downlink_color["transfer"] == 16
        relay.play(1)
        # Another peak, same transfer: nothing changes.
        relay.pipeline.flush(2, 0, DisplayTarget("pq", 600))
        assert relay.pipeline.downlink_mapping["transfer"] == "pq"
        relay.play(2)
        relay.pipeline.flush(3, 0, DisplayTarget("sdr"))
        assert relay.pipeline.downlink_mapping["applied"] == ["dolby_vision"]
        relay.play(3)
        relay.pipeline.flush(4, 0)  # a seek that names no display keeps it
        assert relay.pipeline.downlink_mapping["transfer"] == "sdr"
        streams = {epoch: relay.epoch_stream(epoch) for epoch in range(4)}
    finally:
        relay.close()
    for epoch, tags, rpu in ((0, SDR_TAGS, False), (1, PQ_TAGS, True), (2, PQ_TAGS, True),
                             (3, SDR_TAGS, False)):
        frames, kinds = streams[epoch]
        assert len(frames) == 3 and {_tags(frame) for frame in frames} == {tags}, epoch
        assert (62 in kinds) == rpu, epoch  # the profile 8 RPU rides an unmapped epoch
    # Epoch 3 took the spare that epoch 0 left; it left one of its own.
    assert mapper.spawned == [EpochMapping("sdr", ("dolby_vision",))] * 3


def test_a_seek_does_not_wait_for_the_mapper_to_drain(tmp_path):
    """An uplink runs ahead: the mapper's socket and ffmpeg held seconds of
    packets, and the decode stage, waiting to write the next, saw a seek to
    an unmapped epoch ~7 s late. The seek kills the mapper instead."""
    mapper = _FakeMapper(_StuckProcess)
    relay = _Relay(_dynamic_source(tmp_path, frames=3), mapper, DisplayTarget("sdr"), dolby_vision=P81)
    try:
        info = next(iter(relay.track.packets()))
        relay.pipeline.feed(relay.track.media_packet(info, 0))
        deadline = time.monotonic() + 10
        while not mapper.processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mapper.processes[0].writing.wait(10)
        trace = relay.pipeline.flush(1, 0, DisplayTarget("pq", 1107))
        relay.play(1)
        frames, _kinds = relay.epoch_stream(1)
    finally:
        relay.close()
    assert trace.flush_seen_ms is not None and trace.flush_seen_ms < 2000
    assert len(frames) == 3 and {_tags(frame) for frame in frames} == {PQ_TAGS}
    assert relay.errors == []


def test_sessions_report_the_mapping_at_open_and_on_every_seek(tmp_path, monkeypatch):
    """A server file whose first keyframe carries HDR10+ is mapped for SDR;
    seek_ready says what each epoch is."""
    plain = tmp_path / "plain.mkv"
    _write_source(plain, frames=4)
    with av.open(str(plain)) as source, av.open(str(tmp_path / "hdr10plus.mkv"), "w") as output:
        template = source.streams.video[0]
        stream = output.add_stream_from_template(template)
        length_size = hevc_nal_length_size(bytes(template.codec_context.extradata))
        for packet in source.demux(template):
            if not packet.size:
                continue
            nals = list(iter_nals(bytes(packet), length_size))
            nals.insert(next(i for i, nal in enumerate(nals) if nal_type(nal) < 32),
                        sei_nal(4, _hdr10plus(1000)))
            data = b"".join(len(nal).to_bytes(length_size, "big") + nal for nal in nals)
            rewritten = av.Packet(len(data))
            rewritten.update(data)
            rewritten.pts, rewritten.dts, rewritten.time_base = packet.pts, packet.dts, packet.time_base
            rewritten.is_keyframe, rewritten.stream = packet.is_keyframe, stream
            output.mux(rewritten)
    mapper = _FakeMapper()
    seen = {}

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(),
                             library_roots=[str(tmp_path)], display_mapper=mapper)
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            seen["capability"] = client.capabilities.get("display_mapping")
            info = await client.open_session(SessionConfig(
                path="hdr10plus.mkv", source="server_file", model="passthrough",
                quality_tier="lossless-hevc", display_w=64, display_h=64,
                high_bit_depth="hdr", display_target=ClientTarget("sdr"),
            ))
            seen["opened"] = (info.downlink_mapping, info.downlink_color)
            await client.attach_media()
            await client.play()
            await client.seek(0, display_target=ClientTarget("pq", 1107, 203))
            seen["pq"] = (client.session.downlink_mapping, client.session.downlink_color)
            await client.seek(0, display_target=ClientTarget("sdr"))
            seen["sdr"] = (client.session.downlink_mapping, client.session.downlink_color)
            seen["status"] = server.sessions[client.session.session_id].status()["pipeline"]
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
    sdr_color = {"primaries": 1, "transfer": 1, "matrix": 1, "range": "limited"}
    pq_color = {"primaries": 9, "transfer": 16, "matrix": 9, "range": "limited"}
    mapped = {"transfer": "sdr", "peak_nits": None, "sdr_white_nits": None, "applied": ["hdr10plus"]}
    assert seen["capability"] is True
    assert seen["opened"] == (mapped, sdr_color)
    assert seen["pq"] == ({**mapped, "transfer": "pq", "applied": []}, pq_color)
    assert seen["sdr"] == (mapped, sdr_color)
    assert seen["status"]["display_mapping"] == mapped


def test_without_a_target_or_a_mapper_nothing_is_mapped(tmp_path):
    source = _dynamic_source(tmp_path, frames=2)
    for mapper, target in ((_FakeMapper(), None), (None, DisplayTarget("sdr"))):
        relay = _Relay(source, mapper, target, dolby_vision=P81)
        try:
            assert relay.pipeline.downlink_mapping is None
            assert relay.pipeline.dolby_vision_rpu
            relay.play(0)
            frames, kinds = relay.epoch_stream(0)
        finally:
            relay.close()
        assert {_tags(frame) for frame in frames} == {PQ_TAGS} and 62 in kinds
        if mapper is not None:
            assert mapper.spawned == []


# -- the real thing ------------------------------------------------------------------


def _real_mapper() -> DisplayMapper | None:
    ffmpeg = os.environ.get(display_mapping.FFMPEG_ENV) or shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    mapper = DisplayMapper(ffmpeg)
    return mapper if mapper.probe() is None else None


@pytest.fixture(scope="module")
def real_mapper():
    mapper = _real_mapper()
    if mapper is None:
        pytest.skip("no ffmpeg with libplacebo on Vulkan ($RELAY_FFMPEG or PATH)")
    return mapper


def test_ffmpeg_maps_hdr10plus_to_sdr_frame_for_frame(tmp_path, real_mapper):
    relay = _Relay(_dynamic_source(tmp_path, frames=12), real_mapper, DisplayTarget("sdr"),
                   dolby_vision=None, hdr10plus=True)
    try:
        assert relay.pipeline.downlink_mapping["applied"] == ["hdr10plus"]
        source_pts = relay.play(0)
        relay.pipeline.flush(1, 0, DisplayTarget("pq", 1107))
        relay.play(1)
        relay.pipeline.flush(2, 0, DisplayTarget("sdr"))
        relay.play(2)
        mapped, _ = relay.epoch_stream(0)
        unmapped, _ = relay.epoch_stream(1)
        remapped, _ = relay.epoch_stream(2)
    finally:
        relay.close()
    for frames, tags in ((mapped, SDR_TAGS), (unmapped, PQ_TAGS), (remapped, SDR_TAGS)):
        assert [frame.pts for frame in frames] == source_pts
        assert {_tags(frame) for frame in frames} == {tags}
    # Tone-mapped, not relabelled: the SDR picture differs from the PQ one.
    assert bytes(mapped[0].planes[0]) != bytes(unmapped[0].planes[0])
