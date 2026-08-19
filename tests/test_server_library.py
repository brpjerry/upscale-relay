"""Server-hosted library discovery, media serving, and streaming tests."""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import aiohttp
import av
import numpy as np
import pytest

from relay_client_core import RelayClient, SessionConfig
from relay_server.library import LibraryPathError, MediaLibrary
from relay_server.server import RelayServer
import relay_server.session as session_module
from upscale_cli.encode import DEFAULT_LOSSLESS_HEVC_PROFILE


ROOT = Path(__file__).resolve().parents[1]


def free_port_pair() -> int:
    import random

    # Walk a private range; do not ask the OS for an ephemeral port and then
    # release it before use (that check-then-use pattern races allocation).
    start = random.randrange(40000, 55000, 2)
    for candidate in range(start, start + 400, 2):
        try:
            with socket.socket() as control, socket.socket() as media:
                control.bind(("127.0.0.1", candidate))
                media.bind(("127.0.0.1", candidate + 1))
            return candidate
        except OSError:
            continue
    raise RuntimeError("no free port pair")


@pytest.fixture()
def library_file(tmp_path) -> tuple[Path, Path]:
    sample = ROOT / "tests" / "_sample_stream.mkv"
    if not sample.exists():
        from upscale_cli.sample import make_sample

        make_sample(str(sample), frames=90, width=320, height=180, fps=30)
    library = tmp_path / "library"
    target = library / "Shows" / "Sample.MKV"
    target.parent.mkdir(parents=True)
    shutil.copy2(sample, target)
    (library / "ignore.txt").write_text("not media", encoding="utf-8")
    return library, target


@pytest.fixture()
def multitrack_library_file(tmp_path) -> tuple[Path, Path]:
    """Short Matroska source with independently timestamped H.264 + AAC."""
    library = tmp_path / "library"
    target = library / "Shows" / "WithAudio.mkv"
    target.parent.mkdir(parents=True)
    fps = 30
    sample_rate = 48_000
    frames = 60
    with av.open(str(target), "w") as container:
        video = container.add_stream(
            "libx264", rate=Fraction(fps, 1), options={"crf": "18", "preset": "fast"},
        )
        video.width = 320
        video.height = 180
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("aac", rate=sample_rate)
        audio.layout = "stereo"
        audio.metadata.update({"language": "jpn", "title": "Test tone"})
        audio_samples = 0
        for index in range(frames):
            image = np.zeros((180, 320, 3), dtype=np.uint8)
            image[..., index % 3] = (index * 7) % 255
            frame = av.VideoFrame.from_ndarray(image, format="rgb24").reformat(format="yuv420p")
            frame.pts = index
            frame.time_base = Fraction(1, fps)
            for packet in video.encode(frame):
                container.mux(packet)

            target_samples = (index + 1) * sample_rate // fps
            while audio_samples < target_samples:
                count = 1024
                positions = np.arange(audio_samples, audio_samples + count, dtype=np.float32)
                tone = (0.08 * np.sin(positions * (2 * np.pi * 440 / sample_rate))).astype(np.float32)
                samples = np.stack((tone, tone))
                audio_frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
                audio_frame.sample_rate = sample_rate
                audio_frame.pts = audio_samples
                audio_frame.time_base = Fraction(1, sample_rate)
                for packet in audio.encode(audio_frame):
                    container.mux(packet)
                audio_samples += count
        for packet in video.encode(None):
            container.mux(packet)
        for packet in audio.encode(None):
            container.mux(packet)
    return library, target


def source_pts(path: Path) -> list[int]:
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        return sorted(packet.pts for packet in container.demux(stream) if packet.pts is not None)


def downlink_pts(packets, source_tb_den: int = 1000) -> list[int]:
    import io

    blob = b"".join(packet.payload for packet in packets if packet.payload)
    result = []
    with av.open(io.BytesIO(blob)) as container:
        stream = container.streams.video[0]
        for packet in container.demux(stream):
            if packet.pts is not None:
                result.append(round(float(packet.pts * stream.time_base) * source_tb_den))
    return sorted(result)


def stream_packet_times(path_or_blob, stream_type: str) -> tuple[list[dict], list[float]]:
    import io

    source = io.BytesIO(path_or_blob) if isinstance(path_or_blob, bytes) else str(path_or_blob)
    with av.open(source) as container:
        streams = [stream for stream in container.streams if stream.type == stream_type]
        descriptors = [
            {
                "codec": stream.codec_context.name,
                # Matroska synthesizes DURATION at finalize time; it is not a
                # source track identity field and changes with an epoch cut.
                "metadata": {
                    key: value for key, value in stream.metadata.items()
                    if key.upper() != "DURATION"
                },
            }
            for stream in streams
        ]
        times = sorted(
            float(packet.pts * packet.time_base)
            for packet in container.demux(streams)
            if packet.pts is not None
        )
    return descriptors, times


async def collect(client: RelayClient):
    packets = []
    while True:
        packet = await asyncio.wait_for(client.downlink_queue().get(), timeout=60)
        assert packet is not None
        packets.append(packet)
        client.buffered_ms = 0
        if packet.eos:
            return packets


async def collect_some(client: RelayClient, count: int):
    packets = []
    for _ in range(count):
        packet = await asyncio.wait_for(client.downlink_queue().get(), timeout=60)
        assert packet is not None
        packets.append(packet)
    return packets


def test_library_pages_and_path_sandbox(library_file, tmp_path):
    root, target = library_file
    library = MediaLibrary(root)
    root_page, cursor = library.page()
    assert cursor is None
    assert root_page["children"] == [
        {"type": "directory", "name": "Shows", "path": "Shows", "children": []}
    ]
    shows_page, cursor = library.page("Shows")
    assert cursor is None
    assert shows_page["children"] == [
        {"type": "file", "name": "Sample.MKV", "path": "Shows/Sample.MKV"}
    ]
    assert library.resolve_file("Shows/Sample.MKV") == target.resolve()
    with pytest.raises(LibraryPathError):
        library.resolve_file("../outside.mkv")
    with pytest.raises(LibraryPathError):
        library.resolve_file("Shows\\Sample.MKV")
    with pytest.raises(LibraryPathError):
        library.resolve_file("ignore.txt")


def test_library_pages_are_shallow_sorted_and_sandboxed(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "B Folder").mkdir()
    (root / "A Folder").mkdir()
    (root / "A Folder" / "nested.mkv").write_bytes(b"video")
    (root / "b.mkv").write_bytes(b"video")
    (root / "a.mp4").write_bytes(b"video")
    (root / "ignore.txt").write_text("no")
    library = MediaLibrary(root)

    first, cursor = library.page(limit=2)
    assert [child["name"] for child in first["children"]] == ["A Folder", "B Folder"]
    assert first["children"][0]["children"] == []
    assert cursor == "2"
    second, cursor = library.page(offset=int(cursor), limit=2)
    assert [child["name"] for child in second["children"]] == ["a.mp4", "b.mkv"]
    assert cursor is None
    nested, cursor = library.page("A Folder", limit=10)
    assert nested["children"] == [
        {"type": "file", "name": "nested.mkv", "path": "A Folder/nested.mkv"}
    ]
    assert cursor is None
    with pytest.raises(LibraryPathError):
        library.page("../outside")


def test_library_sort_mtime_newest_first_with_name_tiebreak(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "Old Folder").mkdir()
    (root / "New Folder").mkdir()
    (root / "b old.mkv").write_bytes(b"video")
    (root / "newest.mkv").write_bytes(b"video")
    (root / "B tie.mkv").write_bytes(b"video")
    (root / "a tie.mkv").write_bytes(b"video")
    os.utime(root / "Old Folder", (100, 100))
    os.utime(root / "New Folder", (400, 400))
    os.utime(root / "b old.mkv", (100, 100))
    os.utime(root / "newest.mkv", (300, 300))
    os.utime(root / "B tie.mkv", (200, 200))
    os.utime(root / "a tie.mkv", (200, 200))
    library = MediaLibrary(root)

    assert library.page(sort="name") == library.page()

    page, cursor = library.page(sort="mtime")
    assert cursor is None
    assert [child["name"] for child in page["children"]] == [
        "New Folder", "Old Folder",
        "newest.mkv", "a tie.mkv", "B tie.mkv", "b old.mkv",
    ]

    with pytest.raises(ValueError):
        library.page(sort="bogus")


def test_library_mtime_pagination_walks_full_order(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    for name, stamp in [("a.mkv", 100), ("b.mkv", 300), ("c.mkv", 200)]:
        (root / name).write_bytes(b"video")
        os.utime(root / name, (stamp, stamp))
    library = MediaLibrary(root)

    names, cursor = [], "0"
    while cursor is not None:
        page, cursor = library.page(offset=int(cursor), limit=1, sort="mtime")
        names += [child["name"] for child in page["children"]]
    assert names == ["b.mkv", "c.mkv", "a.mkv"]


def test_capabilities_without_library_advertise_no_sort_keys():
    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair())
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            caps = await client.connect()
            assert caps["library"] is False
            assert caps["muxed_aux_tracks"] is False
            assert caps["attachment_cache"] == 0
            assert caps.get("library_sort", []) == []
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_library_http_range_and_server_source_pts(library_file):
    root, target = library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            caps = await client.connect()
            assert caps["library"] is True
            assert caps["muxed_aux_tracks"] is True
            assert caps["attachment_cache"] == 1
            assert caps["library_sort"] == ["name", "mtime"]
            assert caps["default_resize_algorithm"] == "lanczos"
            assert "area" in caps["resize_algorithms"]
            assert "sinc" in caps["resize_algorithms"]
            assert caps["quality_tiers"] == [
                option["id"] for option in caps["quality_options"]
            ]
            assert caps["quality_options"][1]["label"] == "HEVC ~350 Mbps"
            assert caps["quality_options"][-1]["android_supported"] is False
            async with client._http.get(f"http://127.0.0.1:{server.port}/status") as response:
                assert (await response.json())["lossless_hevc_profile"] == DEFAULT_LOSSLESS_HEVC_PROFILE
            async with client._http.get(
                f"http://127.0.0.1:{server.port}/library"
            ) as response:
                bare_page = await response.json()
                assert response.status == 200
            assert bare_page["tree"]["children"] == [
                {"type": "directory", "name": "Shows", "path": "Shows", "children": []}
            ]
            assert bare_page["next_cursor"] is None
            async with client._http.get(
                f"http://127.0.0.1:{server.port}/library", params={"sort": "name"}
            ) as response:
                assert response.status == 200
                assert await response.json() == bare_page
            async with client._http.get(
                f"http://127.0.0.1:{server.port}/library", params={"sort": "mtime"}
            ) as response:
                assert response.status == 200
            async with client._http.get(
                f"http://127.0.0.1:{server.port}/library", params={"sort": "bogus"}
            ) as response:
                assert response.status == 400
            page = await client.fetch_library_page(limit=1)
            assert page["tree"]["children"][0] == {
                "type": "directory", "name": "Shows", "path": "Shows", "children": [],
            }
            assert page["next_cursor"] is None

            async with client._http.get(
                client.media_url("Shows/Sample.MKV"), headers={"Range": "bytes=0-31"}
            ) as response:
                assert response.status == 206
                assert response.headers["Content-Range"].startswith("bytes 0-31/")
                assert await response.read() == target.read_bytes()[:32]

            session = await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=200, fit_mode="cover",
                resize_algorithm="area",
            ))
            assert client.track is None
            assert session.uplink_token is None
            assert session.time_base is not None
            assert session.duration_s is not None
            assert session.avg_rate is not None
            assert (session.downlink_width, session.downlink_height) == (320, 200)
            assert session.fit_mode == "cover"
            assert session.resize_algorithm == "area"
            await client.attach_media()
            await client.start_uplink()  # deliberate no-op for a server source
            await client.play()
            packets = await collect(client)
            assert packets[0].discontinuity
            assert packets[-1].eos
            assert downlink_pts(packets) == source_pts(target)
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_server_source_muxes_original_audio_into_each_epoch(multitrack_library_file):
    root, target = multitrack_library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Shows/WithAudio.mkv", source="server_file", model="passthrough",
                display_w=320, display_h=180, aux_tracks="muxed",
            ))
            assert session.aux_tracks == "muxed"
            await client.attach_media()
            await client.play()
            packets = await collect(client)
            blob = b"".join(packet.payload for packet in packets if packet.payload)

            source_streams, source_audio = stream_packet_times(target, "audio")
            output_streams, output_audio = stream_packet_times(blob, "audio")
            assert output_streams == source_streams
            assert len(output_audio) == len(source_audio)
            assert output_audio[0] == pytest.approx(source_audio[0], abs=0.002)
            assert output_audio[-1] == pytest.approx(source_audio[-1], abs=0.002)
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_unmuxable_auxiliary_codec_confirms_external_fallback(library_file, monkeypatch):
    root, _target = library_file

    def unsupported(_path):
        raise ValueError("matroska does not support this subtitle codec")

    monkeypatch.setattr(session_module, "AuxiliaryTrack", unsupported)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=180, aux_tracks="muxed",
            ))
            assert session.aux_tracks == "external"
            await client.attach_media()
            await client.play()
            assert (await collect(client))[-1].eos
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_large_attachment_set_confirms_external_fallback(library_file, monkeypatch):
    root, _target = library_file
    instances = []

    class FontHeavyAuxiliaryTrack:
        attachment_bytes = session_module.MAX_MUXED_ATTACHMENT_BYTES + 1

        def __init__(self, _path):
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(session_module, "AuxiliaryTrack", FontHeavyAuxiliaryTrack)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=180, aux_tracks="muxed",
            ))
            assert session.aux_tracks == "external"
            assert instances and instances[0].closed
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_server_source_seek_restarts_muxed_audio_near_target(multitrack_library_file):
    root, _target = multitrack_library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Shows/WithAudio.mkv", source="server_file", model="passthrough",
                display_w=320, display_h=180, aux_tracks="muxed",
            ))
            await client.attach_media()
            await client.play()
            await collect_some(client, 3)

            target_s = 1.0
            target_pts = int(target_s / float(session.time_base))
            await client.seek(target_pts)
            packets = await collect(client)
            assert all(packet.epoch == 1 for packet in packets)
            blob = b"".join(packet.payload for packet in packets if packet.payload)
            _streams, audio_times = stream_packet_times(blob, "audio")
            assert audio_times
            assert target_s - 0.3 <= audio_times[0] <= target_s + 0.1
            assert audio_times[-1] > target_s + 0.5
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())


def test_qt_headless_mpv_reads_muxed_audio_without_external_file(
    multitrack_library_file, monkeypatch,
):
    """Exercise the real Qt/libmpv client, not only the fake GUI adapter."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("qasync")
    root, _target = multitrack_library_file

    async def scenario():
        from PySide6.QtWidgets import QApplication

        from desktop_client.mpv_view import MpvPlayerView
        from desktop_client.options import DesktopOptions

        app = QApplication.instance() or QApplication([])
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        player = None
        try:
            await client.connect()
            session = await client.open_session(SessionConfig(
                path="Shows/WithAudio.mkv", source="server_file", model="passthrough",
                display_w=320, display_h=180, aux_tracks="muxed",
            ))
            await client.attach_media()
            await client.play()
            packets = await collect(client)

            queue = asyncio.Queue()
            for packet in packets:
                queue.put_nowait(packet)
            player = MpvPlayerView(options=DesktopOptions(
                headless=True, no_hwdec=True, settings_scope="test-muxed-audio-mpv",
            ))
            player.start(
                session, queue, session.time_base,
                source_path=None, avg_rate=session.avg_rate,
            )
            deadline = asyncio.get_running_loop().time() + 10.0
            audio_seen = False
            while asyncio.get_running_loop().time() < deadline:
                try:
                    tracks = player.mpv.track_list or []
                    audio_seen = any(track.get("type") == "audio" for track in tracks)
                except Exception:
                    audio_seen = False
                if audio_seen:
                    break
                app.processEvents()
                await asyncio.sleep(0.05)
            assert audio_seen
            assert player._source_path is None
        finally:
            if player is not None:
                player.stop()
                player.mpv.terminate()
            await client.teardown()
            await server.stop()

    # LuaJIT uses a caught Windows SEH exception internally. Pytest's global
    # faulthandler prints it as a fatal stack dump even though mpv continues
    # normally (the repository hard rules document code 0xe24c4a02 as noise).
    import faulthandler

    restore_faulthandler = faulthandler.is_enabled()
    faulthandler.disable()
    try:
        asyncio.run(scenario())
    finally:
        if restore_faulthandler:
            faulthandler.enable()


def test_open_ended_range_streams_a_file_over_two_gibibytes(tmp_path):
    """A remux larger than 2 GiB must stream, not just answer bounded ranges.

    mpv opens external audio/subtitle sources with ``Range: bytes=0-``, so the
    remaining length handed to the sendfile path is the whole file. On Windows
    that used to exceed what TransmitFile accepts and the response was cut off
    after its headers, delivering zero bytes (see the NOSENDFILE note in
    ``relay_server.server``). Reading the first chunk is enough: the failure was
    immediate, not partway through the transfer.
    """
    library = tmp_path / "library"
    library.mkdir()
    huge = library / "Huge.mkv"
    huge.touch()
    if sys.platform == "win32":
        # Mark sparse before extending, so the test costs no real disk.
        subprocess.run(["fsutil", "sparse", "setflag", str(huge)],
                       check=False, capture_output=True)
    with open(huge, "r+b") as handle:
        handle.truncate((1 << 31) + 4096)

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(library))
        await server.start()
        try:
            async with aiohttp.ClientSession() as http:
                url = f"http://127.0.0.1:{server.port}/media/Huge.mkv"
                async with http.get(url, headers={"Range": "bytes=0-"}) as response:
                    assert response.status == 206
                    assert response.headers["Content-Range"].endswith(f"/{(1 << 31) + 4096}")
                    assert len(await response.content.readexactly(65536)) == 65536
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_source_seek_restarts_reader_at_new_epoch(library_file):
    root, target = library_file

    async def scenario():
        server = RelayServer(str(ROOT / "models"), free_port_pair(), library_root=str(root))
        await server.start()
        client = RelayClient("127.0.0.1", server.port)
        try:
            await client.connect()
            await client.open_session(SessionConfig(
                path="Shows/Sample.MKV", source="server_file", model="passthrough",
                display_w=320, display_h=180,
            ))
            await client.attach_media()
            await client.play()
            await collect_some(client, 3)

            expected = source_pts(target)
            target_pts = expected[len(expected) // 2]
            await client.seek(target_pts)
            packets = await collect(client)
            assert all(packet.epoch == 1 for packet in packets)
            assert packets[0].discontinuity
            assert packets[-1].eos
            assert downlink_pts(packets) == [pts for pts in expected if pts >= target_pts]
        finally:
            await client.teardown()
            await server.stop()

    asyncio.run(scenario())
