from fractions import Fraction
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import av
import pytest

from relay_media import AuxiliaryTrack
import relay_media.demux as demux_mod
from relay_media.subtitles import SubtitleIndex


@pytest.fixture
def subtitle_movie(tmp_path):
    script = tmp_path / "events.ass"
    script.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 32
PlayResY: 32
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,12,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,Long event
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,Last event
""")
    movie = tmp_path / "events.mkv"
    with av.open(script) as source, av.open(movie, "w") as output:
        video = output.add_stream("libx264", rate=24, options={"g": "24", "bf": "0", "sc_threshold": "0", "tune": "zerolatency"})
        video.width = video.height = 32
        video.pix_fmt = "yuv420p"
        sub = output.add_stream_from_template(source.streams.subtitles[0])
        packets = [packet for packet in source.demux(source.streams.subtitles[0]) if packet.size]
        for index in range(288):
            while packets and float(packets[0].pts * packets[0].time_base) <= index / 24:
                packet = packets.pop(0)
                packet.stream = sub
                output.mux(packet)
            frame = av.VideoFrame(32, 32, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts, frame.time_base = index, Fraction(1, 24)
            output.mux(video.encode(frame))
        output.mux(video.encode(None))
    return movie


def test_long_event_survives_forward_backward_and_repeated_seeks(subtitle_movie):
    track = AuxiliaryTrack(str(subtitle_movie))
    assert track._subtitle_index is None  # no startup scan or spool allocation
    try:
        for target in (7, 3, 7):
            packets = list(track.packets(target))
            assert [(p.packet.pts, p.packet.duration) for p in packets] == [(0, 10000), (10000, 2000)]
            assert b"Long event" in bytes(packets[0].packet)
        assert [p.packet.pts for p in track.packets(10.5)] == [10000]
        path = track._subtitle_index.path
        assert path.exists()
    finally:
        track.close()
    assert not path.parent.exists()


def test_normal_playback_indexes_without_extra_source_scan(subtitle_movie, monkeypatch):
    track = AuxiliaryTrack(str(subtitle_movie))
    try:
        list(track.packets())
        monkeypatch.setattr(demux_mod.av, "open", lambda *_a, **_k: pytest.fail("unexpected source rescan"))
        assert [p.packet.pts for p in track.packets(7)] == [0, 10000]
    finally:
        track.close()


def test_stopping_during_catchup_waits_for_reader_then_deletes_spool(subtitle_movie, monkeypatch):
    track = AuxiliaryTrack(str(subtitle_movie))
    entered, release = threading.Event(), threading.Event()
    original = track._remember_subtitle_progress
    paths = []

    def remember(packet, **kwargs):
        original(packet, **kwargs)
        if packet.stream.type == "subtitle" and not entered.is_set():
            paths.append(track._subtitle_index.path)
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(track, "_remember_subtitle_progress", remember)
    iterator = track.packets(7)
    result = []
    reader = threading.Thread(target=lambda: result.append(next(iterator, None)), daemon=True)
    reader.start()
    assert entered.wait(1)
    closer = threading.Thread(target=track.close, daemon=True)
    closer.start()
    for _ in range(100):
        if track._iter_gen > 1:
            break
        time.sleep(0.001)
    assert closer.is_alive()  # it cannot close the native reader in flight
    release.set()
    reader.join(2)
    closer.join(2)
    assert not reader.is_alive() and not closer.is_alive()
    assert result == [None]
    assert not paths[0].parent.exists()
    assert track._subtitle_index is None


def test_close_with_active_replay_cursor_removes_temp_files(subtitle_movie):
    track = AuxiliaryTrack(str(subtitle_movie))
    iterator = track.packets(7)
    assert next(iterator).packet.pts == 0
    path = track._subtitle_index.path
    track.close()
    iterator.close()
    assert not path.parent.exists()


def test_index_restores_payload_timestamps_and_side_data(tmp_path):
    with av.open(tmp_path / "template.mkv", "w") as container:
        stream = container.add_stream("ass")
        packet = av.Packet(b"subtitle packet")
        packet.stream = stream
        packet.pts, packet.dts, packet.duration = 100, 90, 500
        packet.time_base = Fraction(1, 1000)
        side = av.packet.PacketSideData(av.packet.packet_sidedata_type_from_literal("new_extradata"), 3)
        side.update(b"abc")
        packet.set_sidedata(side)
        index = SubtitleIndex()
        try:
            index.remember(packet)
            cursor = index.overlapping(0.3)
            row = cursor.fetchone()
            cursor.close()
            _key, restored = index.restore(row, container.streams)
            assert bytes(restored) == bytes(packet)
            assert (restored.pts, restored.dts, restored.duration, restored.time_base) == (100, 90, 500, Fraction(1, 1000))
            assert [(s.data_type, bytes(s)) for s in restored.iter_sidedata()] == [("new_extradata", b"abc")]
        finally:
            index.close()


def test_subtitle_spool_has_hard_disk_bound(tmp_path):
    with av.open(tmp_path / "template.mkv", "w") as container:
        stream = container.add_stream("ass")
        index = SubtitleIndex(max_bytes=32 * 1024)
        try:
            with pytest.raises(RuntimeError, match="limit"):
                for position in range(20):
                    packet = av.Packet(b"x" * 4096)
                    packet.stream = stream
                    packet.pts, packet.duration, packet.time_base = position, 100, Fraction(1, 1000)
                    index.remember(packet)
            assert index.path.stat().st_size <= 32 * 1024
        finally:
            index.close()


def test_stateful_bitmap_subtitles_choose_external_mode_before_playback(monkeypatch):
    stream = SimpleNamespace(type="subtitle", codec_context=SimpleNamespace(name="hdmv_pgs_subtitle"))
    class Streams(list):
        video = []
    closed = []
    monkeypatch.setattr(demux_mod.av, "open", lambda _path: SimpleNamespace(
        streams=Streams([stream]), close=lambda: closed.append(True),
    ))
    with pytest.raises(ValueError, match="external media"):
        AuxiliaryTrack("bitmap.mkv")
    assert closed == [True]


def test_replayed_long_subtitle_survives_actual_epoch_mux(subtitle_movie):
    import asyncio
    import io
    from relay_client_core import RelayClient, SessionConfig
    from relay_server.server import RelayServer
    from test_server_library import collect, free_port_pair

    async def scenario():
        server = RelayServer("missing-models", free_port_pair(), library_root=str(subtitle_movie.parent))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            opened = await client.open_session(SessionConfig(
                source="server_file", path=subtitle_movie.name, model="passthrough",
                quality_tier="lossless-ffv1", display_w=32, display_h=32, aux_tracks="muxed",
            ))
            assert opened.aux_tracks == "muxed"
            await client.attach_media()
            await client.seek(7000)
            await client.play()
            packets = await collect(client)
            blob = b"".join(p.payload for p in packets if p.epoch == 1)
            with av.open(io.BytesIO(blob)) as container:
                subtitle_packets = [p for p in container.demux(container.streams.subtitles[0]) if p.size]
                assert [(p.pts, p.duration) for p in subtitle_packets] == [(0, 10000), (10000, 2000)]
            with av.open(io.BytesIO(blob)) as container:
                video_pts = [p.pts for p in container.demux(container.streams.video[0]) if p.pts is not None]
                assert min(video_pts) >= 7000
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
